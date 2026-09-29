#!/usr/bin/env bash
# One-time model download into the `ollama_models` volume.
# This is the ONLY step that needs internet access; after it, the sandbox runs offline.
#
# Usage: docker compose up -d db ollama && ./scripts/pull_models.sh
#        ./scripts/pull_models.sh --native   # dev option: pull into native Ollama on the Mac
set -euo pipefail

cd "$(dirname "$0")/.."

# Pick up model names from .env if present, else use the defaults.
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi
EMBED_MODEL="${EMBED_MODEL:-nomic-embed-text}"
LLM_MODEL="${LLM_MODEL:-llama3.2:3b}"

if [[ "${1:-}" == "--native" ]]; then
  # Native Ollama keeps its own model store (~/.ollama), separate from the Docker volume.
  command -v ollama >/dev/null || { echo "ollama CLI not found. Install it from https://ollama.com/download" >&2; exit 1; }
  for model in "$EMBED_MODEL" "$LLM_MODEL"; do
    echo "==> Pulling $model (native)"
    ollama pull "$model"
  done
  ollama list
  exit 0
fi

if ! docker compose ps --status running --services | grep -qx ollama; then
  echo "Ollama container is not running. Start it first: docker compose up -d db ollama" >&2
  exit 1
fi

for model in "$EMBED_MODEL" "$LLM_MODEL"; do
  echo "==> Pulling $model"
  docker compose exec ollama ollama pull "$model"
done

echo "==> Models available in the ollama_models volume:"
docker compose exec ollama ollama list
