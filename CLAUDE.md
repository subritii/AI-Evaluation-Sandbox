# CLAUDE.md

## Project

Secure Enterprise AI Evaluation Sandbox: a self-hosted gateway and dashboard that lets bank and fintech compliance teams test an LLM workflow offline. Pipeline: mock data → Trust Engine (PII masking) → pgvector RAG → local LLM (Ollama) → per-stage latency tracking → audits and report. See README.md for architecture and the build roadmap.

## Why this project exists

This is a portfolio project for Solutions Engineer roles. The owner is building it to learn the concepts deeply and to demo and defend it in interviews. So:

- **Explain before building.** At the start of each task, briefly explain the plan and key concepts, then write code.
- **Explain tradeoffs.** When choosing an approach, name the main alternative and why this one fits.
- **Keep code readable over clever.** Clear names, docstrings on public functions, comments where the "why" isn't obvious.
- **Work in small, testable steps.** After each step, say how to verify it works.

## Stack

- Python 3.11+, FastAPI, Pydantic settings
- PostgreSQL 16 + pgvector (`pgvector/pgvector:pg16` image), psycopg / SQLAlchemy
- Ollama: `llama3.2:3b` for generation, `nomic-embed-text` for embeddings (default provider); any OpenAI-compatible endpoint can replace either via `LLM_PROVIDER` / `EMBED_PROVIDER` (`backend/app/rag/model_client.py`)
- Microsoft Presidio analyzer with custom recognizers; masking (typed, numbered placeholders) is our own code in `trust_engine/engine.py`
- LangChain for text splitting only; files load directly (pypdf for PDFs), since the langchain-community loaders are deprecated
- Streamlit dashboard
- pytest, Faker, numpy
- Docker Compose

## Non-negotiable rules

1. **Never log raw PII.** Do not log request or response bodies before they pass through the Trust Engine. No `print()` of user text in the gateway.
2. **Anonymize before embedding.** Nothing is written to the vector table without passing through the Trust Engine.
3. **No external network calls at runtime by default.** Embeddings and generation go to a model server the operator controls: Ollama by default, or an OpenAI-compatible endpoint chosen in `.env` (Task 9). The default all-Docker configuration must pass `scripts/airgap_check.py`. An endpoint outside the sandbox network is an explicit opt-in (`docker-compose.external-endpoint.yml`), recorded as `model_backend=remote`, and reported as not isolated. No telemetry and no third-party calls from code. (Pulling models and building images during setup is the only other exception.)
4. **Honest metrics.** Never hardcode or fabricate results. Every number shown in the dashboard or report must come from a real run and be reproducible from a script.
5. **Label simulations.** Anything injected or simulated (e.g., fault injection) must be labeled as such in data and UI.
6. **Tenant scoping.** Once Task 5 lands, every query touching documents must run with a tenant context; rely on Postgres row-level security, not only app-level filtering.
7. **Secrets in `.env`** only; keep `.env.example` updated; never commit `.env`.

## Conventions

- Backend code lives in `backend/app/`, organized by `trust_engine/`, `rag/`, `metrics/`, `db/`.
- The dashboard lives in `dashboard/` (its own container). It talks only to the gateway over HTTP and never gets database credentials; logic lives in plain modules (`batch.py`, `reports.py`, `report_html.py`) so it's testable and runnable without the UI. Streamlit caches imported modules: restart the container after editing them.
- Standalone evaluation scripts live in `scripts/` and write outputs to `reports/`.
- Config comes from environment variables via `app/config.py`. Inside Compose, reach services by name (`db`, `ollama`), not `localhost`.
- Network: data-handling services stay on the `internal: true` `sandbox` network with no published ports; only `proxy` publishes (loopback). Don't add ports or non-internal networks to other services; rerun `airgap_check.py` after compose changes. The native-Ollama override is not isolated.
- Tests in `backend/tests/`, named `test_<module>.py`. Add tests with every Trust Engine change, including cases that should NOT be flagged.
- Latency: use `time.perf_counter()`; record stages `pii_scan`, `retrieval`, `time_to_first_token`, `generation`, `total`, in milliseconds.

## Commands

Operations, configuration reference, and fixes for known setup issues: `docs/runbook.md`.

```bash
./scripts/pull_models.sh                # one-time model pull (setup network; the only internet step)
docker compose up -d                    # start everything (ingests, then isolated stack + proxy)
.venv/bin/python scripts/airgap_check.py  # prove no route out; saves a report
docker compose run --rm backend pytest  # run tests
docker compose run --rm --no-deps dashboard pytest  # dashboard tests
docker compose run --rm tools python scripts/eval_rag.py  # scripts needing DB/Ollama run inside the sandbox
docker compose logs -f backend          # backend logs
```

## Current focus

Update this line as work progresses: **Tasks 1-4, 6-9, and 11 done (+ lean Task 3). All-Docker stack isolated and proven by `airgap_check.py`; models switchable between Ollama and any OpenAI-compatible endpoint (Task 9, tested end to end via Ollama's /v1 with the air-gap intact); runbook in `docs/runbook.md`. Next: Task 5 (multi-tenancy with Postgres RLS). Task 10 is not defined yet. Optional: a 200-request all-Docker canary run (~2 h on CPU).**
