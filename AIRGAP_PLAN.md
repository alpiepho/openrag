# Air-Gap Implementation Plan

## Goal

Enable OpenRAG to run fully air-gapped (no internet access) with a single `airgap` toggle in the TUI, keeping changes minimal and avoiding new Docker networks.

When `airgap: true`:
1. **Only Ollama is a valid provider** — never instantiate OpenAI/Anthropic/WatsonX clients
2. **Telemetry is disabled** — `DO_NOT_TRACK=true` reaches the backend container
3. **tiktoken doesn't phone home** — handled gracefully without internet
4. **No OpenAI API key leaks into Langflow** — even if previously stored

---

## Architecture Summary

```
User sets "airgap: true" in TUI
        │
        ▼
┌──────────────────────┐
│  config.yaml         │  airgap: true  (new top-level field)
│  .env file           │  AIRGAP=true, DO_NOT_TRACK=true,
│                      │  OPENAI_API_KEY='', ANTHROPIC_API_KEY='',
│                      │  WATSONX_API_KEY=''
└────────┬─────────────┘
         │
         ▼
┌──────────────────────┐
│  docker-compose.yml  │  Passes AIRGAP + DO_NOT_TRACK to backend & langflow
└────────┬─────────────┘
    ┌────┴─────┐
    ▼          ▼
┌────────┐ ┌────────┐
│Backend │ │Langflow│  Backend: telemetry disabled, tiktoken safe
│        │ │        │  Langflow: OPENAI_API_KEY=None, only Ollama used
└────────┘ └────────┘
```

---

## Changes Required (6 files)

### Change 1: `config/config.yaml` — Add `airgap` field

Add a new top-level boolean field.

```yaml
airgap: false          # ← NEW: set true for air-gapped environments
agent:
  llm_model: ...
```

**Why top-level:** It's a deployment-mode flag, not a provider or knowledge setting. Keeping it at the root makes it obvious and easy to find.

**Lines changed:** 1 line added.

---

### Change 2: `src/config/config_manager.py` — Support the new field

Add `airgap` to the `OpenRAGConfig` dataclass so ConfigManager reads it from YAML and exposes it to the backend.

```python
@dataclass
class OpenRAGConfig:
    airgap: bool = False          # ← NEW
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    # ... existing fields ...
```

Also add an env-var override in `_apply_env_overrides`:

```python
# Air-gap mode
airgap_val = os.environ.get("AIRGAP", "").lower()
if airgap_val in ("true", "1", "yes", "on"):
    config.airgap = True
```

When `config.airgap is True`, force provider constraints:

```python
if config.airgap:
    # In airgap mode, only Ollama is valid — blank out cloud provider keys
    config.providers.openai.configured = False
    config.providers.openai.api_key = ""
    config.providers.anthropic.configured = False
    config.providers.anthropic.api_key = ""
    config.providers.watsonx.configured = False
    config.providers.watsonx.api_key = ""
    # Force embedding/LLM to Ollama if set to a cloud provider
    if config.knowledge.embedding_provider != "ollama":
        logger.warning(f"Airgap mode: overriding embedding_provider from "
                       f"'{config.knowledge.embedding_provider}' to 'ollama'")
        config.knowledge.embedding_provider = "ollama"
    if config.agent.llm_provider != "ollama":
        logger.warning(f"Airgap mode: overriding llm_provider from "
                       f"'{config.agent.llm_provider}' to 'ollama'")
        config.agent.llm_provider = "ollama"
```

**Lines changed:** ~20 lines added to `config_manager.py`.

---

### Change 3: `src/tui/config_fields.py` — Add TUI toggle for airgap

Add a new section (or a field in the existing "Advanced" section) for the airgap toggle.  Since all TUI fields are currently string-based, model it the same way as other boolean-ish fields (like `LANGFLOW_AUTO_LOGIN`):

```python
ConfigSection(
    title="Air-Gap Mode",
    fields=[
        ConfigField(
            label="Air-Gap Mode",
            attribute="airgap",
            env_var="AIRGAP",
            default="false",
            helper_text="Set to 'true' to disable all internet-dependent features "
                        "(telemetry, OpenAI, Anthropic, WatsonX). Only Ollama will be used.",
        ),
    ],
),
```

Place this as the **first** section in `CONFIG_SECTIONS` so it's immediately visible.

**Lines changed:** ~12 lines added.

---

### Change 4: `src/tui/managers/env_manager.py` — Write AIRGAP + DO_NOT_TRACK to `.env`

**4a. Add fields to `EnvConfig` dataclass:**

```python
@dataclass
class EnvConfig:
    airgap: str = "false"         # ← NEW
    # ... existing fields ...
```

**4b. Add env-var mapping in `ENV_VAR_ATTRS`:**

```python
"AIRGAP": "airgap",
```

**4c. In `save_env_file`, write the new variables:**

At the top of the file output (after the header comment), add:

```python
# Air-Gap Mode
f.write(f"AIRGAP={_quote(self.config.airgap)}\n")
if self.config.airgap.lower() in ("true", "1", "yes"):
    # Force telemetry off and blank cloud provider keys
    f.write(f"DO_NOT_TRACK='true'\n")
    f.write(f"OPENAI_API_KEY=''\n")
    f.write(f"ANTHROPIC_API_KEY=''\n")
    f.write(f"WATSONX_API_KEY=''\n")
    f.write(f"WATSONX_ENDPOINT=''\n")
    f.write(f"WATSONX_PROJECT_ID=''\n")
else:
    f.write(f"DO_NOT_TRACK='false'\n")
```

This ensures that when airgap is on:
- `DO_NOT_TRACK=true` is always in the `.env` (solving the "doesn't pass through" problem)
- Cloud API keys are forcibly blanked even if the user previously entered one
- The TUI can't accidentally re-inject a stale OpenAI key

**Important:** Because the TUI overwrites `.env` on every save, these values will persist correctly through TUI restarts.

**Lines changed:** ~20 lines added.

---

### Change 5: `docker-compose.yml` — Pass `DO_NOT_TRACK` and `AIRGAP` to containers

Add two lines to the `openrag-backend` environment block:

```yaml
  openrag-backend:
    environment:
      # ... existing vars ...
      - DO_NOT_TRACK=${DO_NOT_TRACK:-false}     # ← NEW
      - AIRGAP=${AIRGAP:-false}                 # ← NEW
```

Add to the `langflow` environment block:

```yaml
  langflow:
    environment:
      # ... existing vars ...
      - AIRGAP=${AIRGAP:-false}                 # ← NEW
```

Also add `AIRGAP` to `LANGFLOW_VARIABLES_TO_GET_FROM_ENVIRONMENT`:

```yaml
      - LANGFLOW_VARIABLES_TO_GET_FROM_ENVIRONMENT=...,AIRGAP
```

This solves the core problem: `DO_NOT_TRACK` now actually reaches the backend container where `is_do_not_track()` checks `os.environ`. No code change needed to the telemetry client itself.

**Lines changed:** 3 lines added, 1 line modified.

---

### Change 6: `src/services/document_service.py` — tiktoken graceful fallback

The backend uses `tiktoken.encoding_for_model()` and `tiktoken.get_encoding()` which download encoding files on first use. When air-gapped and the cache is cold, this will throw a network error.

Add a safe wrapper:

```python
def _get_tiktoken_encoding(model: str = None):
    """Get tiktoken encoding, with graceful fallback for air-gapped environments."""
    model = model or get_embedding_model()
    try:
        return tiktoken.encoding_for_model(model)
    except (KeyError, Exception):
        pass
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        logger.warning(
            "tiktoken encoding unavailable (air-gapped or cache cold). "
            "Falling back to approximate token counting (chars/4)."
        )
        return None


def get_token_count(text: str, model: str = None) -> int:
    """Get token count using tiktoken, with char-based fallback."""
    encoding = _get_tiktoken_encoding(model)
    if encoding is None:
        return len(text) // 4  # Rough approximation: ~4 chars per token
    return len(encoding.encode(text))
```

This way, if tiktoken can't download its data files, the system still works with an approximate token count rather than crashing or stalling.

**Lines changed:** ~15 lines modified.

---

## What This Does NOT Change

| Concern | Approach |
|---------|----------|
| Docker network topology | No change — no `internal: true` network added |
| Langflow flow JSON files | No change — the EmbeddingModelComponent code is untouched. With `OPENAI_API_KEY=''` passed to Langflow, the OpenAI branch will fail fast (no key) instead of retrying against `api.openai.com` with a valid key |
| Langflow database | Not cleared automatically. If a stale API key is persisted in Langflow's DB, the `OPENAI_API_KEY=''` environment variable override should take precedence since `LANGFLOW_VARIABLES_TO_GET_FROM_ENVIRONMENT` includes it |
| Frontend code | No changes needed |
| Ollama proxy/setup | No changes — Ollama at `10.0.0.60:11434` stays as-is |
| New Python dependencies | None added |

---

## Why This Is Sufficient (Without Patching Flow Components)

The key insight from the analysis: **the OpenAI embedding objects are created because a real `OPENAI_API_KEY` is being injected.** When we force `OPENAI_API_KEY=''` (or `None`), the `OpenAIEmbeddings` constructor in the Langflow flow will either:

1. **Not be selected** — because `embedding_provider: ollama` in config.yaml routes to the Ollama branch
2. **Fail immediately** if somehow instantiated — no API key means no retries against `api.openai.com`, so no 60-second stalls

Combined with `DO_NOT_TRACK=true` killing telemetry (saves ~10s per event) and tiktoken fallback (saves ~20s on cold cache), the total air-gap penalty drops from **~2m30s to near zero**.

---

## Implementation Order

| Step | File | Risk | Effort |
|------|------|------|--------|
| 1 | `docker-compose.yml` | Very low — additive only | 5 min |
| 2 | `config/config.yaml` | Very low — additive only | 2 min |
| 3 | `src/config/config_manager.py` | Low — new field + enforcement logic | 15 min |
| 4 | `src/tui/config_fields.py` | Low — new field definition | 5 min |
| 5 | `src/tui/managers/env_manager.py` | Medium — touches .env generation | 15 min |
| 6 | `src/services/document_service.py` | Low — adds fallback path | 10 min |

**Total estimated effort: ~1 hour**

---

## Testing Plan

1. **Unit test:** Set `AIRGAP=true` in env, load ConfigManager, assert all cloud providers are `configured: false` and `embedding_provider == "ollama"`
2. **TUI test:** Toggle airgap in TUI, save, verify `.env` contains `AIRGAP='true'`, `DO_NOT_TRACK='true'`, `OPENAI_API_KEY=''`
3. **Docker test:** `docker compose up -d`, then `docker exec openrag-backend env | grep DO_NOT_TRACK` → should show `true`
4. **Integration test:** Disconnect internet, upload PDF, verify ingestion completes in < 60s
5. **Regression test:** Set `airgap: false`, verify OpenAI/Anthropic providers work normally with valid keys

---

## Lingering Risk: Langflow DB Stale Key

If a real OpenAI API key was previously persisted in Langflow's internal SQLite database (as the analysis suggests), the environment variable `OPENAI_API_KEY=''` *should* override it because Langflow prioritizes env vars listed in `LANGFLOW_VARIABLES_TO_GET_FROM_ENVIRONMENT` over its database. However, if it doesn't:

**Fallback fix (one-time manual step):**
```bash
docker compose down
# Remove Langflow's persistent state (will re-import flows on next start)
docker volume ls | grep langflow  # identify the volume
docker volume rm <langflow_volume>
docker compose up -d
```

This can be documented in a troubleshooting section but shouldn't be needed if the env var override works correctly.
