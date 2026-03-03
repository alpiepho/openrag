# OpenRAG Air-Gap Failure Analysis

## Problem

When running OpenRAG with all 3 local systems disconnected from the internet, PDF file upload and processing **stalls for ~2m30s** until the network cable is re-plugged. The system is configured to use only local Ollama for LLM/embeddings, yet it still tries to reach external internet services.

---

## Root Causes Identified

### 🚨 1. OpenAI Embedding Objects Created Even When Not Selected (PRIMARY)

**The Langflow ingestion flow eagerly creates ALL embedding provider objects** (OpenAI + Ollama) during every flow execution — regardless of which provider is actually selected.

Even though your config says:

```yaml
embedding_model: nomic-embed-text:v1.5-8kcontext
embedding_provider: ollama
openai:
  configured: false
```

The Langflow `EmbeddingModelComponent` still builds `OpenAIEmbeddings` client objects. Evidence from `docker logs langflow`:

```
Vertex EmbeddingModel-3LsIP → OpenAIEmbeddings(
    model='text-embedding-3-small',
    tiktoken_enabled=True,
    max_retries=3,
    retry_min_seconds=4,
    retry_max_seconds=20
)
```

These objects attempt to connect to `https://api.openai.com` during ingestion. With `max_retries=3` and exponential backoff (4s–20s), each failed OpenAI call blocks for up to **~60 seconds** before giving up.

**Why is this happening?** A real OpenAI API key (`sk-proj-HF8iy...`) is being injected into Langflow via the HTTP header `x-langflow-global-var-openai_api_key`, causing Langflow to treat OpenAI as a valid provider. This key is **not** in `~/.openrag/tui/.env` or `config.yaml` — it's likely stored in Langflow's internal database or was set as a host environment variable at some point.

### 🟡 2. tiktoken Downloads Tokenizer Files from the Internet

The `OpenAIEmbeddings` objects have `tiktoken_enabled=True` (LangChain default). On first use, tiktoken downloads encoding files from:

```
https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken
```

If this file isn't cached inside the container, it blocks until the download succeeds or times out.

### 🟡 3. Telemetry Phones Home to Scarf

The backend sends telemetry to `https://langflow.gateway.scarf.sh` on every significant event:

| Event | When |
|-------|------|
| `ORB_APP_START_INIT` | App startup |
| `ORB_APP_STARTED` | App ready |
| `ORB_TASK_CREATED` | PDF upload starts |
| `ORB_TASK_COMPLETE` | PDF processing finishes |
| `ORB_FLOW_BACKUP_COMPLETE` | Periodic flow backup |
| `ORB_FLOW_SETTINGS_REAPPLIED` | Settings change |

**Timeouts:** 10s connect + 15s read per attempt.

The telemetry client (`src/utils/telemetry/client.py`) is fire-and-forget (async) and handles `ConnectError`/`TimeoutException` without retrying — so it won't block the main processing for long. But each failed connection still burns up to **10 seconds** on the initial connect timeout before giving up.

### 🟢 4. Google Drive Connector Polling (Minor)

The frontend polls `/connectors` every ~3 seconds, which triggers a `google_drive` connector creation attempt. This fails immediately with a config error (no OAuth key), so it does **not** cause internet-dependent blocking — just log noise.

---

## What the Stall Timeline Looks Like

| Time Offset | What Happens |
|-------------|-------------|
| 0s | PDF upload, task created |
| 0s | Telemetry `ORB_TASK_CREATED` → tries `langflow.gateway.scarf.sh` → 10s connect timeout |
| 0s | Langflow ingestion flow starts |
| 0–5s | Docling converts PDF to text (local, works fine) |
| 5s | `EmbeddingModel-3LsIP` builds `OpenAIEmbeddings` → tiktoken tries to download `cl100k_base.tiktoken` from Azure blob → blocks |
| 5–25s | tiktoken download timeout / OpenAI client init delays |
| 25s | OpenSearch multi-embedding component tries to use OpenAI embeddings alongside Ollama → `api.openai.com` unreachable |
| 25–90s | OpenAI retries: 3 attempts × ~20s max backoff each |
| 90–150s | Finally falls back to Ollama-only path, ingestion completes |
| 150s | Task succeeds (~2m 32s total, matching your observed `2m 32s` duration) |

---

## Fixes

### Option A: Quick Fix — Disable Telemetry + Remove OpenAI Key (Recommended)

**Step 1:** Add `DO_NOT_TRACK=true` to your `.env`:

```bash
echo "DO_NOT_TRACK=true" >> ~/.openrag/tui/.env
```

**Step 2:** Ensure `OPENAI_API_KEY` is explicitly blank in your `.env`:

```bash
echo "OPENAI_API_KEY=''" >> ~/.openrag/tui/.env
```

**Step 3:** Clear the key from Langflow's persisted database. The simplest way:

```bash
# Stop all containers
docker compose down

# Clear Langflow's variable store (this resets persisted global vars)
docker exec langflow rm -f /app/data/langflow.db  # if using SQLite
# OR for the OpenRAG-managed Langflow:
docker volume inspect and clear the relevant volume

# Restart
docker compose up -d
```

**Step 4:** Verify the key is gone after restart:

```bash
docker logs langflow 2>&1 | grep -i "OPENAI_API_KEY"
# Should show empty or no matches
```

**Expected improvement:** Ingestion should drop from ~2m30s to ~30s or less.

### Option B: Block Internet at the Docker Network Level

If you want a hard guarantee that no container reaches the internet:

```yaml
# In docker-compose.yml, add to each service:
services:
  openrag-backend:
    networks:
      - openrag-internal
    # Remove any 'default' network
  langflow:
    networks:
      - openrag-internal
  # ... same for all services

networks:
  openrag-internal:
    driver: bridge
    internal: true  # <-- This blocks all external internet access
```

> **Note:** With `internal: true`, containers can talk to each other but NOT to the internet. Your Ollama at `10.0.0.60:11434` is on the LAN, so you'd need to keep the `default` (non-internal) network for the Ollama connection, or run Ollama as a Docker service in the same compose file.

### Option C: Pre-Warm tiktoken Cache

If you ever need OpenAI embeddings alongside Ollama, pre-populate the tiktoken cache while internet is available:

```bash
# Download the tiktoken encoding file
mkdir -p /tmp/tiktoken_cache
curl -o /tmp/tiktoken_cache/cl100k_base.tiktoken \
  https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken

# Mount it into the Langflow container via docker-compose.yml:
# langflow:
#   environment:
#     - TIKTOKEN_CACHE_DIR=/app/tiktoken_cache
#   volumes:
#     - /tmp/tiktoken_cache:/app/tiktoken_cache:ro
```

### Option D: Patch the Ingestion Flow to Skip Unconfigured Providers

This is a code-level fix to the `EmbeddingModelComponent` in `flows/ingestion_flow.json` (the inline Python). The component should check whether a provider is actually configured before creating its client objects. This would prevent OpenAI/WatsonX clients from ever being instantiated when only Ollama is configured.

This would need to be done in the Langflow flow definition or the `lfx` package's `EmbeddingModelComponent` class.

---

## Verification Checklist

After applying fixes, verify air-gap works:

- [ ] Disconnect internet cable
- [ ] `docker compose down && docker compose up -d`
- [ ] Wait for all containers to be healthy
- [ ] Upload a PDF via the OpenRAG UI
- [ ] Confirm ingestion completes in < 60 seconds
- [ ] Check logs: `docker logs openrag-backend 2>&1 | grep -i "telemetry"` → should show `DO_NOT_TRACK is enabled`
- [ ] Check logs: `docker logs langflow 2>&1 | grep -i "openai"` → should NOT show OpenAI embedding objects being created

---

## Summary Table

| Issue | Internet Target | Impact | Fix |
|-------|----------------|--------|-----|
| OpenAI embeddings created eagerly | `api.openai.com` | **~2 min blocking** (retries + backoff) | Remove `OPENAI_API_KEY`, clear from Langflow DB |
| tiktoken encoding download | `openaipublic.blob.core.windows.net` | ~20s blocking (first use) | Pre-cache or remove OpenAI embeddings |
| Telemetry to Scarf | `langflow.gateway.scarf.sh` | ~10s per event (async) | `DO_NOT_TRACK=true` |
| Google Drive connector | N/A (fails locally) | None (just log noise) | Ignore or remove connector config |
