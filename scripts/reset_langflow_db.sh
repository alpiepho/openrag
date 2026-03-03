#!/usr/bin/env bash
# reset_langflow_db.sh — Wipe Langflow's persisted SQLite database.
#
# The Langflow container stores its DB inside the container filesystem
# (no named volume). Removing and recreating the container is enough to
# clear any cached API keys and stale flow state.
#
# Usage:  ./scripts/reset_langflow_db.sh
#         (run from the openrag project root where docker-compose.yml lives)

set -euo pipefail

COMPOSE_FILE="${1:-docker-compose.yml}"

echo "==> Stopping and removing langflow container (wipes internal SQLite DB)..."
docker compose -f "$COMPOSE_FILE" down --remove-orphans langflow 2>/dev/null || true
# Belt-and-suspenders: force-remove by name if compose didn't catch it
docker rm -f langflow 2>/dev/null || true

echo "==> Recreating langflow container with fresh database..."
docker compose -f "$COMPOSE_FILE" up -d langflow

echo "==> Done.  Langflow DB has been reset."
echo "    Any previously persisted API keys are gone."
echo "    The flows in /app/flows will be re-loaded from the host mount."
