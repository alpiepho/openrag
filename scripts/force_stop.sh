#!/usr/bin/env bash
# force_stop.sh — Hard-stop all OpenRAG services without needing a .env file.
#
# Use this when the TUI is stuck or `docker compose down` fails because
# environment variables are missing.
#
# Usage:
#   bash scripts/force_stop.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENRAG_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Stopping OpenRAG services ==="

# --- 1. Stop docling-serve (native process) ---
echo "Stopping docling-serve..."
uv run --directory "$OPENRAG_DIR" python "$SCRIPT_DIR/docling_ctl.py" stop 2>/dev/null \
  && echo "  docling stopped." \
  || echo "  docling was not running."

# --- 2. Kill any stuck 'uv run openrag' TUI processes ---
echo "Killing any stuck openrag TUI processes..."
pkill -f 'uv run openrag' 2>/dev/null && echo "  openrag TUI killed." || echo "  No stuck TUI found."

# --- 3. Stop docker containers by known names (works without .env) ---
CONTAINERS=(langflow osdash os openrag-frontend openrag-backend ollama-proxy)
echo "Stopping Docker containers..."
for c in "${CONTAINERS[@]}"; do
    if docker inspect "$c" &>/dev/null; then
        docker stop "$c" && docker rm "$c" && echo "  $c stopped and removed."
    fi
done

# --- 4. Fallback: try docker compose down if a .env exists ---
if [[ -f "$OPENRAG_DIR/.env" ]]; then
    echo "Running 'docker compose down' as final cleanup..."
    docker compose --project-directory "$OPENRAG_DIR" --env-file "$OPENRAG_DIR/.env" down 2>/dev/null \
      || true
fi

echo ""
echo "=== All OpenRAG services stopped. ==="
