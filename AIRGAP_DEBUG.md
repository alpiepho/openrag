# Air-Gap Debug Analysis — Why It Still Stalls

## TL;DR

The previous changes (config_manager airgap enforcement, env_manager blanking keys, docker-compose `DO_NOT_TRACK` passthrough, tiktoken fallback) addressed the **backend** side. But the **actual stall happens inside the Langflow container**, which runs its own Python code from `ingestion_flow.json` — and that code is completely unaffected by our backend-side changes.

The flow JSON has **two EmbeddingModel nodes hardcoded to `provider: "OpenAI"`** which Langflow eagerly executes. These nodes still receive the `OPENAI_API_KEY` via the `X-LANGFLOW-GLOBAL-VAR-OPENAI_API_KEY` header OR from Langflow's persisted database, causing `OpenAIEmbeddings` objects to call `api.openai.com` and block.

---

## What We Fixed (Working Correctly)

| Change | Status | Verification |
|--------|--------|-------------|
| `DO_NOT_TRACK` passed to backend container | ✅ Working | `docker exec openrag-backend env \| grep DO_NOT_TRACK` → `true` |
| `AIRGAP` passed to backend + langflow | ✅ Working | `docker exec openrag-backend env \| grep AIRGAP` → `true` |
| Config manager blanks cloud provider keys | ✅ Working | Backend's `config_manager` returns empty API keys |
| TUI writes `OPENAI_API_KEY=''` in `.env` | ✅ Working | `.env` file has blank key |
| tiktoken fallback in document_service.py | ✅ Working | Falls back to char/4 approximation |

**These fixes are correct but only affect the backend Python process.** The actual PDF ingestion happens inside Langflow's container.

---

## Root Cause: The Ingestion Runs Inside Langflow, Not the Backend

The document processing pipeline works like this:

```
User uploads PDF
       │
       ▼
┌─────────────────────┐
│  openrag-backend    │  Receives upload, creates task
│  (our Python code)  │  config_manager.airgap = True ← WE FIXED THIS
└────────┬────────────┘
         │ POST /api/v1/run/{flow_id}
         │ + X-LANGFLOW-GLOBAL-VAR-OPENAI_API_KEY header  ← PROBLEM 1
         ▼
┌─────────────────────┐
│  langflow container │  Runs ingestion_flow.json
│  (separate process) │  Has its OWN Python runtime
│                     │  Does NOT read our config_manager
└────────┬────────────┘
         │ Executes 3 EmbeddingModel components:
         │
    ┌────┼────────────────────┐
    ▼    ▼                    ▼
EAo9i  3LsIP              E0hvR
OpenAI  OpenAI             Ollama
(stalls) (stalls)          (works)
```

### The 3 EmbeddingModel Nodes in `ingestion_flow.json`

| Node ID | Provider | API Key Source | fail_safe_mode | What Happens Offline |
|---------|----------|---------------|----------------|---------------------|
| `EmbeddingModel-EAo9i` | **OpenAI** | `OPENAI_API_KEY` (load_from_db=True) | **True** | Creates `OpenAIEmbeddings` → tries `api.openai.com` → retries 3× → **~60s stall** → returns None |
| `EmbeddingModel-3LsIP` | **OpenAI** | `OPENAI_API_KEY` (load_from_db=True) | **False** | Creates `OpenAIEmbeddings` → tries `api.openai.com` → retries 3× → **~60s stall** → raises exception |
| `EmbeddingModel-E0hvR` | **Ollama** | N/A | True | Creates `OllamaEmbeddings` → calls local Ollama → **works fine** |

**All 3 nodes execute on every ingestion**, regardless of which provider you selected in config.yaml. Langflow runs the entire flow graph. The OpenAI nodes have `fail_safe_mode` which means they'll eventually return `None` instead of crashing — but only after timing out against `api.openai.com`.

---

## Problem 1: Backend Still Sends OpenAI API Key to Langflow

**File:** `src/utils/langflow_headers.py` — `add_provider_credentials_to_headers()`

```python
# This runs during every ingestion request:
if config.providers.openai.api_key:
    headers["X-LANGFLOW-GLOBAL-VAR-OPENAI_API_KEY"] = str(config.providers.openai.api_key)
```

Even though `config_manager` blanks the key when `airgap=True`, there's a **race condition / source issue**: the `OPENAI_API_KEY` environment variable is set in docker-compose.yml from the `.env` file. The `settings.py` code at line 327 reads `os.getenv("OPENAI_API_KEY")` and the `patched_async_client` property loads it into `os.environ` at line 440.

**BUT** — even if the header sends an empty key, the `OPENAI_API_KEY` value `"OPENAI_API_KEY"` in the flow node template has `load_from_db: true`. This means Langflow resolves it from its **internal variable store** (SQLite database or environment). If Langflow's variable store has a previously-set key, it will use that regardless of what we send in headers.

---

## Problem 2: Langflow's Persisted Variable Store

Langflow stores global variables in its internal database. When a real `OPENAI_API_KEY` was set previously (via the TUI or environment), Langflow persisted it. Even after blanking it in `.env`, Langflow's database still has the old key.

The `LANGFLOW_VARIABLES_TO_GET_FROM_ENVIRONMENT` mechanism is supposed to make environment variables override DB values. But:

- docker-compose.yml passes `OPENAI_API_KEY=${OPENAI_API_KEY:-None}` to langflow
- When `.env` has `OPENAI_API_KEY=''`, the compose substitution `${OPENAI_API_KEY:-None}` falls through to `None` since empty string is "unset" in compose
- So Langflow sees `OPENAI_API_KEY=None` (the literal string "None"), which might not override the DB value properly

---

## Problem 3: `patched_async_client` HTTP/2 Probe (Backend-Side)

**File:** `src/config/settings.py` — `patched_async_client` property (line ~490)

The backend's lazy-initialized OpenAI client performs an HTTP/2 probe on first access:

```python
async def probe_http2():
    client = AsyncOpenAI()  # Uses OPENAI_API_KEY from env
    await asyncio.wait_for(
        client.embeddings.create(model='text-embedding-3-small', input=['test']),
        timeout=5.0
    )
```

This fires a **real API call to `api.openai.com`** the first time any code accesses `clients.patched_async_client` or `clients.patched_embedding_client`. In air-gap mode, this will stall for 5 seconds (the timeout), then fall back. But it shouldn't happen at all in air-gap mode.

This is triggered during **search** operations (not ingestion), but it's still a problem if you search while air-gapped.

---

## Problem 4: `OpenAIEmbeddings-joRJ6` Hardcoded Component ID in Backend

**File:** `src/api/langflow_files.py` (line 106) and `src/services/langflow_file_service.py` (line 303)

```python
# The backend sends tweaks to a component named "OpenAIEmbeddings-joRJ6"
if "OpenAIEmbeddings-joRJ6" not in tweaks:
    tweaks["OpenAIEmbeddings-joRJ6"] = {}
tweaks["OpenAIEmbeddings-joRJ6"]["model"] = settings["embeddingModel"]
```

This sends an embedding model name tweak to the `OpenAIEmbeddings-joRJ6` component — which is the **OpenSearch Multi-Model Multi-Embedding** component that references the OpenAI embedding node. Even when using Ollama, the backend is tweaking an OpenAI-named component. This component ID references the OpenAI embedding path inside the flow graph.

---

## Required Fixes (Revised)

### Fix 1: `langflow_headers.py` — Don't Send Cloud Provider Credentials in Air-Gap Mode

The simplest, most impactful fix. When air-gapped, don't send `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `WATSONX_*` headers to Langflow at all.

```python
def add_provider_credentials_to_headers(headers, config):
    # In air-gap mode, skip all cloud provider credentials
    if config.airgap:
        # Only send Ollama endpoint
        if config.providers.ollama.endpoint:
            ollama_endpoint = transform_localhost_url(config.providers.ollama.endpoint)
            headers["X-LANGFLOW-GLOBAL-VAR-OLLAMA_BASE_URL"] = str(ollama_endpoint)
        return
    # ... existing code for non-airgap mode ...
```

### Fix 2: `settings.py` — Skip HTTP/2 Probe in Air-Gap Mode

When `AIRGAP=true`, don't run the OpenAI HTTP/2 probe. Create the client with HTTP/1.1 directly (or skip client creation entirely if Ollama-only).

```python
@property
def patched_async_client(self):
    # ... existing lock logic ...
    
    # In air-gap mode, skip the HTTP/2 probe entirely
    config = get_openrag_config()
    if config.airgap:
        http_client = httpx.AsyncClient(http2=False, timeout=httpx.Timeout(60.0, connect=10.0))
        self._patched_async_client = patch_openai_with_mcp(AsyncOpenAI(http_client=http_client))
        logger.info("Air-gap mode: OpenAI client initialized with HTTP/1.1 (no probe)")
        return self._patched_async_client
    
    # ... existing probe logic for non-airgap mode ...
```

### Fix 3: Docker-compose — Force `OPENAI_API_KEY` to Empty String in Langflow

Change the Langflow service's `OPENAI_API_KEY` default from `None` to empty when air-gapped. Since we can't conditionally set defaults in compose, the `.env` file approach is the right one — but the compose default fallback needs to be empty, not `None`:

Current: `OPENAI_API_KEY=${OPENAI_API_KEY:-None}`
Problem: When `.env` sets `OPENAI_API_KEY=''`, compose treats empty as "unset" and substitutes `None`.

**Fix:** Use `OPENAI_API_KEY=${OPENAI_API_KEY-None}` (note: single dash, not `:-`). The `:-` syntax treats empty as unset; the `-` syntax only substitutes for truly undefined variables.

Or better: explicitly set `OPENAI_API_KEY=''` as a real value in the env_manager output using a marker value, e.g., `OPENAI_API_KEY='NONE'`.

### Fix 4: One-Time Langflow Database Cleanup

If a real OpenAI API key is persisted in Langflow's SQLite database, it needs to be cleared. This is a one-time manual step:

```bash
docker compose down
# Find and remove Langflow's DB volume
docker volume ls | grep langflow
docker volume rm <langflow_volume_name>
# OR if using bind mount, remove the DB file:
# rm ~/.openrag/data/langflow.db
docker compose up -d
```

### Fix 5: Consider Skipping `patched_async_client` Creation Entirely in Air-Gap Mode

Since the `patched_async_client` is only needed for the direct embedding/LLM path and the search embedding path, and in air-gap mode all routing goes through LiteLLM → Ollama anyway, the client can still be created but must never probe OpenAI.

---

## The Stall Timeline (Revised)

| Time | What Happens | Where | Air-Gap Impact |
|------|-------------|-------|----------------|
| 0s | PDF uploaded to backend | Backend | None |
| 0s | Backend calls `POST /api/v1/run/{flow_id}` to Langflow | Backend→Langflow | None (local) |
| 0s | Backend sends `X-LANGFLOW-GLOBAL-VAR-OPENAI_API_KEY` header | Backend→Langflow | **🔴 Sends key even in airgap** |
| 1s | Langflow starts flow execution | Langflow | None |
| 1-5s | Docling converts PDF to text | Langflow→Docling | None (local) |
| 5s | **EmbeddingModel-EAo9i** (OpenAI, fail_safe=True) builds `OpenAIEmbeddings` | Langflow | **🔴 Tries api.openai.com** |
| 5s | **EmbeddingModel-3LsIP** (OpenAI, fail_safe=False) builds `OpenAIEmbeddings` | Langflow | **🔴 Tries api.openai.com** |
| 5s | **EmbeddingModel-E0hvR** (Ollama, fail_safe=True) builds `OllamaEmbeddings` | Langflow | ✅ Works |
| 5-65s | OpenAI nodes retry 3× with backoff against `api.openai.com` | Langflow | **🔴 ~60s stall per node** |
| 65-125s | Second OpenAI node also retries | Langflow | **🔴 ~60s stall** |
| 125-150s | Flow completes (EAo9i returns None via fail_safe, 3LsIP may error) | Langflow | Finally done |
| 150s | Backend receives result | Backend | Total: ~2m30s |

## Priority Order for Fixes

| # | Fix | Impact | Effort | Risk |
|---|-----|--------|--------|------|
| 1 | `langflow_headers.py`: Don't send cloud creds in airgap | **Eliminates key injection** | 5 min | Very low |
| 2 | `settings.py`: Skip HTTP/2 probe in airgap | **Eliminates 5s backend stall** | 10 min | Low |
| 3 | `docker-compose.yml`: Fix empty-string compose substitution | **Ensures Langflow env is blank** | 5 min | Low |
| 4 | One-time: Clear Langflow DB | **Removes persisted key** | Manual, 2 min | Medium (resets Langflow state) |
| 5 | Flow JSON: Consider adding `AIRGAP` awareness | Would skip OpenAI nodes entirely | Complex | High (modifies vendor flow) |

**Fixes 1-3 together should eliminate the stall.** Fix 4 is a one-time manual step as a safety net. Fix 5 is optional — with fixes 1-3, the OpenAI nodes will have no API key, hit the `if not api_key` check in `build_embeddings()`, and return `None` immediately (fail_safe) or raise (non-fail-safe) without any network call.

---

## Why Config Manager Airgap Enforcement Alone Is Insufficient

The config manager correctly blanks provider keys in the backend process. But:

1. **Langflow is a separate container** — it doesn't import or call `config_manager.py`. It has its own runtime.
2. **Langflow gets credentials via HTTP headers and environment variables** — not from config.yaml.
3. **Langflow has a persisted variable store** — even if we stop sending headers, old values persist.
4. **The flow JSON has provider=OpenAI hardcoded** — nodes are configured statically, not dynamically from config.yaml.

The fix must operate at the **boundary between backend and Langflow**: the headers sent during `run_ingestion_flow()` and the environment variables in `docker-compose.yml`.
