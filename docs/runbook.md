# Runbook

How to install, run, configure, and fix the sandbox. Numbers marked
*measured* come from the build machine: Apple M1, 8 GB RAM, macOS 26.4,
Docker Desktop (engine 29.6) with an 8 GB VM and 8 CPUs.

Contents: [Hardware](#hardware-requirements) · [Install](#install) ·
[Operate](#day-to-day-operations) · [Configurations](#configurations-all-docker-vs-native-ollama) ·
[Config reference](#configuration-reference) · [Known issues](#known-issues) ·
[Reset and recovery](#reset-and-recovery)

---

## Hardware requirements

| | Minimum | Recommended | Measured on the build machine |
|---|---|---|---|
| RAM | 8 GB | 16 GB | 8 GB Mac; backend ~745 MB (spaCy model loaded); llama3.2:3b ~2.5 GB when loaded; db, dashboard, proxy < 50 MB each |
| Docker VM memory | 6 GB | 8 GB+ | 7.75 GB (nearly all of an 8 GB Mac, so native apps compete for memory) |
| CPU | 4 cores | 8 cores | 8; host load average reached **28.6** during all-Docker CPU inference |
| Free disk | **25 GB** before install | 40 GB | images 10.1 GB + volumes 2.4 GB; `Docker.raw` 15 GB on disk; native Ollama models another 2.3 GB |
| GPU | none | Apple Silicon (native Ollama) or NVIDIA | Docker on macOS can't use the Apple GPU |

What the numbers mean in practice (*measured*):

| Configuration | Time per answer | End-to-end P95 |
|---|---|---|
| Native Ollama (Apple GPU) | 1.8-8.3 s | 3.3 s (n=200) |
| All-Docker (CPU) | 7.4-45 s | 26.7 s (n=20) |

Keep at least **10 GB free while running** evaluations. The long runs in this
project stopped if free space fell below that. Free space fell from 22 GB to
12 GB over one day of work, mostly Docker's disk image growing.

---

## Install

```bash
# 0. Prerequisites: Docker Desktop running; Python 3.11+ for host-side scripts
git clone <repo-url> ~/code/ai-eval-sandbox     # NOT inside iCloud Drive (see Known issues)
cd ~/code/ai-eval-sandbox
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt

# 1. Configure: set POSTGRES_PASSWORD; leave the rest at defaults
cp .env.example .env

# 2. One-time model pull (the only step that needs the internet)
./scripts/pull_models.sh

# 3. Start everything (one command): ingest, gateway, dashboard, proxy
docker compose up -d

# 4. Verify
docker compose ps                                # all healthy; ingest "Exited (0)"
.venv/bin/python scripts/airgap_check.py         # expect Verdict: PASS
open http://localhost:8501                       # dashboard
```

The first start takes a minute or two (*measured*: 87 s): the backend waits for
the one-shot `ingest` to embed the policy documents.

---

## Day-to-day operations

| Task | Command |
|---|---|
| Start / stop | `docker compose up -d` / `docker compose down` (keeps volumes) |
| Status | `docker compose ps` |
| Logs | `docker compose logs -f backend` (never contains request bodies) |
| Which config is running? | `curl -s localhost:8000/info` → backend, provider and endpoint per model, models, scrubber, load |
| Tests | `docker compose run --rm backend pytest` and `docker compose run --rm --no-deps dashboard pytest` |
| RAG eval | `docker compose run --rm tools python scripts/eval_rag.py` |
| Detector eval | `.venv/bin/python scripts/generate_dataset.py && .venv/bin/python scripts/eval_detector.py` |
| Canary audit | `.venv/bin/python scripts/canary_audit.py` (add `--ollama-log ollama.log` with native Ollama) |
| Air-gap check | `.venv/bin/python scripts/airgap_check.py` (rerun after any compose change) |
| Batch without the UI | `docker compose run --rm dashboard python batch.py /data/sample_batches/policy_questions.jsonl` |
| Free disk | `df -h .` and `docker system df` |
| CI status | README badge, or `gh run list --limit 5` |
| Reproduce CI locally (empty DB, no Ollama) | create a throwaway DB, then `docker compose run --rm -e CI=true -e POSTGRES_DB=<it> -e OLLAMA_BASE_URL=http://nowhere.invalid:11434 backend pytest -rs` |

All reports land in `reports/` (gitignored) and record their git commit.
Commit before a run you intend to cite, or the report says `-dirty`.

---

## Configurations: all-Docker vs native Ollama

| | All-Docker (default) | Native Ollama (dev option) |
|---|---|---|
| Start | `docker compose up -d` | `ollama serve`, then `docker compose -f docker-compose.yml -f docker-compose.native-ollama.yml up -d` |
| Model runtime | `ollama` container, CPU only on macOS | Ollama app on the Mac, Apple GPU (Metal) |
| Speed | 7-45 s per answer | 2-8 s per answer |
| Network isolation | **Yes**: `airgap_check.py` → PASS | **No**: backend, ingest, tools join `edge` to reach the host; `airgap_check.py` → FAIL |
| Recorded as | `model_backend=docker` | `model_backend=native` |
| Models | `ollama_models` Docker volume (`./scripts/pull_models.sh`) | `~/.ollama` (`./scripts/pull_models.sh --native`) |
| Use for | the demo, the air-gap proof, isolation claims | fast iteration, larger latency samples |

**Switching.** Run `docker compose down` first, then start the other
configuration. Ingest runs on every start, so stored and query embeddings
always come from the same runtime.

**The `COMPOSE_FILE` trap.** If `COMPOSE_FILE=docker-compose.yml:docker-compose.native-ollama.yml`
is exported in your shell or set in `.env`, *every* `docker compose` command
uses the native configuration, including the ones in this runbook. Check with
`echo $COMPOSE_FILE` and `grep COMPOSE_FILE .env`. The symptom is `/info`
reporting `native` when you expected `docker`, or the air-gap check failing.

### Using an OpenAI-compatible endpoint

Set in `.env` (per model; embeddings and generation can differ):

```bash
LLM_PROVIDER=openai_compatible
EMBED_PROVIDER=openai_compatible        # or keep ollama for embeddings
OPENAI_BASE_URL=http://my-vllm:8000/v1
OPENAI_API_KEY=                          # if the server needs one
LLM_MODEL=<model name on that server>
```

| Where the endpoint runs | How to start | Isolated? |
|---|---|---|
| A container on the `sandbox` network (e.g. vLLM added to Compose), or Ollama's own `/v1` | `docker compose up -d` | **Yes**: air-gap check PASS (tested with `http://ollama:11434/v1`) |
| On this Mac (LM Studio, llama.cpp server) | `docker compose -f docker-compose.yml -f docker-compose.external-endpoint.yml up -d`, base URL `http://host.docker.internal:<port>/v1`, `MODEL_BACKEND=native` | No |
| Another machine (private vLLM, Azure OpenAI) | same override, `MODEL_BACKEND=remote` (the default there) | No, and masked text leaves this machine |

Things to check:

- Embeddings must be 768-dimensional (`db/schema.sql`), or ingest stops with a
  dimension error. Changing dimension means changing the schema and
  re-ingesting.
- The nomic task prefixes (`search_query: ` / `search_document: `) are added
  for every provider. They help nomic models and are harmless text for others,
  but retrieval quality with a different embedding model must be re-measured
  with `eval_rag.py`.
- Ingest runs on every start, so switching `EMBED_PROVIDER` re-embeds the policies
  through the new endpoint automatically.
- The `ollama` container still starts even if neither model uses it.

**Labels are not optional.** Every latency sample and report records the
backend, and the dashboard and HTML report print it next to every latency
number. Never compare numbers across configurations without saying so.

---

## Configuration reference

Values come from, in priority order: **(1)** the `environment:` block in
`docker-compose.yml` (inside containers), **(2)** real environment variables,
**(3)** `.env`, **(4)** the default in `backend/app/config.py`. Compose also
reads `.env` to fill `${VAR:-default}` placeholders. See
[.env overrides code defaults](#env-overrides-code-defaults).

| Variable | Default | Used by | Notes |
|---|---|---|---|
| `POSTGRES_PASSWORD` | *none (required)* | db, backend, ingest, tools | Compose refuses to start without it. Secret: `.env` only |
| `POSTGRES_USER` / `POSTGRES_DB` | `sandbox` / `sandbox` | db, backend | |
| `POSTGRES_HOST` / `POSTGRES_PORT` | `localhost` / `5432` | backend settings | Compose sets `db` / `5432` inside containers; a `.env` value only affects host-run code, and the DB has no host port |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | backend settings | Compose sets `http://ollama:11434` (all-Docker) or `http://host.docker.internal:11434` (native) |
| `MODEL_BACKEND` | `unknown` | every latency sample and report | Where the model server runs: `docker` / `native` / `remote`; set by the compose files. Host-run code records `unknown` unless set |
| `LLM_PROVIDER` | `ollama` | generation | `ollama` or `openai_compatible` |
| `EMBED_PROVIDER` | `ollama` | embeddings (ingest and queries) | `ollama` or `openai_compatible`; independent of `LLM_PROVIDER` |
| `OPENAI_BASE_URL` | empty | OpenAI-compatible client | Required if either provider is `openai_compatible`, e.g. `http://ollama:11434/v1`. Recorded in reports with credentials stripped |
| `OPENAI_API_KEY` | empty | OpenAI-compatible client | **Secret.** Sent only as an `Authorization` header; never logged, returned by `/info`, or written to reports |
| `LLM_MODEL` | `llama3.2:3b` | generation, pull script | Changing it needs `./scripts/pull_models.sh` |
| `EMBED_MODEL` | `nomic-embed-text` | embeddings, pull script | Must produce 768-dim vectors (`embed_dim`), or change `db/schema.sql` and re-ingest |
| `LLM_TEMPERATURE` | `0` | generation | 0 = greedy, reproducible answers |
| `LLM_MAX_TOKENS` | `256` | generation | Caps answer length and tail latency |
| `BACKEND_HOST_PORT` | `8000` | proxy | Loopback port for the API |
| `DASHBOARD_HOST_PORT` | `8501` | proxy | Loopback port for the dashboard |
| `COMPOSE_FILE` | unset | docker compose | Set to use the native override by default; see the trap above |
| `POLICIES_DIR` | `data/policies` | ingest | Compose sets `/data/policies` |
| `GATEWAY_URL` | `http://localhost:8000` | dashboard, `batch.py` | Compose sets `http://backend:8000` |
| `REPORTS_DIR` | `<repo>/reports` | dashboard | Compose sets `/reports` |

Code-only settings (no env var in `.env.example`, but overridable by name):
`CHUNK_SIZE=1000`, `CHUNK_OVERLAP=150`, `EMBED_DIM=768`.

Unused keys that older `.env` files may still contain:
`POSTGRES_HOST_PORT`, `OLLAMA_HOST_PORT` (the DB and Ollama no longer publish
ports). They're harmless but misleading; delete them.

---

## Known issues

Each entry: symptom → cause → fix. "Seen here" marks what happened on the
build machine.

### Low disk space, and Docker's disk not shrinking

- **Symptom:** free space keeps falling; builds or model pulls fail; Docker
  Desktop becomes unstable.
- **Cause:** Docker Desktop stores everything (images, volumes, build cache)
  in one disk image,
  `~/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw`. It
  grows as you pull and build. When you delete things inside Docker, the
  file can take a while to shrink on the Mac. *Seen here:* `docker builder prune`
  freed 1.7 GB inside Docker, but macOS free space didn't change right away;
  `Docker.raw` stayed at 15 GB.
- **Fix:**
  1. Check: `df -h .`, `docker system df`, `du -sh ~/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw`.
  2. Safe cleanup: `docker builder prune -f` (build cache; rebuilds then need
     the internet for pip), `docker image prune -f` (dangling images).
  3. Cap growth in Docker Desktop → Settings → Resources → Advanced → disk usage limit.
  4. Don't delete `Docker.raw` by hand while Docker is running.
  5. Keep ≥ 10 GB free during evaluation runs.

### Corrupted Docker disk image

- **Symptom:** *reported during development:* Docker Desktop's disk became
  corrupted. Typical signs: Docker Desktop won't start or keeps restarting,
  or containers fail with I/O or filesystem errors.
- **Likely cause:** the Mac's disk filling up while Docker was writing, or a
  hard shutdown mid-write.
- **Fix, least destructive first:**
  1. Free disk space (≥ 10 GB), quit Docker Desktop fully, start it again.
  2. Docker Desktop → Troubleshoot → **Restart**.
  3. Docker Desktop → Troubleshoot → **Clean / Purge data**. ⚠️ **Deletes all
     images, containers, and volumes**, including the Postgres data and the
     downloaded models. Afterwards: `./scripts/pull_models.sh` and
     `docker compose up -d` rebuild everything. Nothing in this project lives
     only in Docker: code and reports are in the repo folder, and the
     database is rebuilt by ingest.
  4. Last resort: Troubleshoot → **Reset to factory defaults** (also resets settings).

### iCloud-synced folders and Spotlight load

- **Symptom:** the Mac is slow, load average is high before anything runs,
  latency numbers are noisy. *Seen here:* a session was interrupted by
  iCloud sync load while the project was in iCloud Drive. After the move, the
  first native runs still saw host load 5-11 on 8 CPUs and a later rerun saw
  2-4 (recorded in each report; the cause of the difference wasn't isolated).
- **Cause:** a project inside iCloud Drive (including `~/Desktop` and
  `~/Documents` when "Desktop & Documents Folders" sync is on) makes iCloud
  upload every generated file: `reports/`, `.venv`, `__pycache__`, and
  `ollama.log` (which grows by about 1 MB per 200 requests). Spotlight then
  indexes the same churn (`mds`, `mds_stores`, `mdworker` in Activity Monitor).
- **Fix:**
  1. Keep the repo outside iCloud Drive, e.g. `~/code/ai-eval-sandbox`. Check
     with `pwd` (a path under `~/Library/Mobile Documents/` or a synced
     Desktop/Documents is iCloud).
  2. Exclude the repo from Spotlight: System Settings → Spotlight →
     Search Privacy (on older macOS: Siri & Spotlight → Spotlight Privacy) →
     add the folder.
  3. Before a latency run, check `uptime`. Every report records load average
     before and after, so a noisy run can at least be identified.

### Port conflicts (local Postgres, other dev servers)

- **Symptom:** `docker compose up` fails with "ports are not available" or
  "address already in use".
- **Cause:** something else holds the port. In older versions of this project
  the DB published `5432`, which clashes with a Homebrew or Postgres.app
  server. In the current configuration **the DB, Ollama, backend, and
  dashboard publish no ports**; only the proxy publishes `8000` and `8501`.
- **Fix:**
  1. Find the owner: `lsof -nP -iTCP:8000 -sTCP:LISTEN` (same for 8501, 5432).
  2. Change the port in `.env`: `BACKEND_HOST_PORT=8001`, `DASHBOARD_HOST_PORT=8502`.
  3. Or stop the other server: `brew services list` / `brew services stop postgresql@16`.
  4. Native Ollama on `11434` no longer conflicts: the `ollama` container
     publishes nothing.

### Docker Desktop won't launch

- **Symptom:** the whale icon stays on "Starting…", the app quits, or
  `docker` commands report the daemon isn't running.
- **Fix, in order:**
  1. Check free disk (Docker needs room to start its VM).
  2. Quit fully (menu bar → Quit Docker Desktop, or `killall Docker`), wait,
     reopen.
  3. Restart the Mac (clears stuck virtualization processes).
  4. Read the logs: `~/Library/Containers/com.docker.docker/Data/log/`.
  5. Troubleshoot → Clean / Purge data, then Reset to factory defaults (both
     destructive; see the corrupted-disk section).
  6. Reinstall Docker Desktop (your data in `Docker.raw` is removed).

### `.env` overrides code defaults

- **Symptom:** a setting you changed in code (or a new default) has no
  effect, or host-run scripts behave differently from containers.
- **Cause:** `.env` beats the defaults in `config.py`, and it's never updated
  for you. Keys you copied months ago keep winning, and new keys you never
  copied silently fall back to defaults. *Seen here:* the working `.env` was
  missing `MODEL_BACKEND`, `LLM_TEMPERATURE`, `LLM_MAX_TOKENS`,
  `BACKEND_HOST_PORT`, and `DASHBOARD_HOST_PORT` (so code defaults applied,
  and host-run scripts recorded `model_backend=unknown` unless it was typed
  on the command line). It still had `POSTGRES_HOST_PORT` and
  `OLLAMA_HOST_PORT`, which nothing reads any more. On 2026-09-30 it was
  rebuilt from `.env.example` with only `POSTGRES_PASSWORD` carried over.
- **Fix:**
  1. Compare keys with the example (prints names only, no values):
     `comm -3 <(grep -oE '^[A-Z_]+' .env.example | sort -u) <(grep -oE '^[A-Z_]+' .env | sort -u)`
     (left column: missing from `.env`; right column: only in `.env`).
  2. See what containers actually get: `docker compose config | grep -A30 'backend:'`.
  3. See what the running gateway uses: `curl -s localhost:8000/info`.
  4. Remember that the compose `environment:` block wins inside containers
     (e.g. `POSTGRES_HOST: db`), so a `.env` value can look ignored there.

### `docker compose config` prints secrets

- `docker compose config` resolves `.env` into plain text, including
  `POSTGRES_PASSWORD` and `OPENAI_API_KEY`. Pipe it through `grep` for the
  keys you need, and don't paste its full output into tickets or chats.

### Streamlit shows old code after an edit

- **Cause:** Streamlit reloads `app.py` on refresh but keeps imported modules
  (`batch.py`, `reports.py`, `report_html.py`) cached.
- **Fix:** `docker compose restart dashboard`.

### Reports cite a `-dirty` commit

- **Cause:** uncommitted (or untracked) files in the repo when the script
  started. Scripts record the commit at the start of a run (the code that
  runs is the code checked out then), so edits during a run don't change it.
- **Fix:** commit first, then rerun.

---

## Reset and recovery

| Goal | Command | Destroys |
|---|---|---|
| Restart the stack | `docker compose down && docker compose up -d` | nothing |
| Rebuild images after dependency changes | `docker compose build && docker compose --profile tools build tools` | nothing (needs internet for pip/apt) |
| Re-embed policies | `docker compose run --rm ingest` (also runs on every `up`) | old chunks, replaced |
| Wipe the database | `docker compose down && docker volume rm ai-eval-sandbox_pgdata` | all chunks and latency samples |
| Re-download models | `docker volume rm ai-eval-sandbox_ollama_models && ./scripts/pull_models.sh` | Docker model store |
| Start completely fresh | Docker Desktop → Troubleshoot → Clean / Purge data, then Install steps 2-4 | everything in Docker |

`reports/` lives in the repo folder, not in Docker, so none of the above
deletes past reports.
