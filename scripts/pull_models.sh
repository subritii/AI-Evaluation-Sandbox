#!/usr/bin/env bash
# One-time model download into the `ollama_models` volume.
# This is the ONLY step that needs internet access; after it, the sandbox runs offline.
#
# The running `ollama` container sits on the isolated `sandbox` network and
# can't download anything, so the pull runs in a separate one-off container
# (`ollama-pull`, profile `setup`) on its own network, sharing the model volume.
#
# Usage: ./scripts/pull_models.sh
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

echo "==> Pulling $EMBED_MODEL and $LLM_MODEL into the ollama_models volume (setup network)"
docker compose --profile setup run --rm ollama-pull
