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
- Ollama: `llama3.2:3b` for generation, `nomic-embed-text` for embeddings
- Microsoft Presidio analyzer with custom recognizers; masking (typed, numbered placeholders) is our own code in `trust_engine/engine.py`
- LangChain for text splitting only; files load directly (pypdf for PDFs), since the langchain-community loaders are deprecated
- Streamlit dashboard
- pytest, Faker, numpy
- Docker Compose

## Non-negotiable rules

1. **Never log raw PII.** Do not log request or response bodies before they pass through the Trust Engine. No `print()` of user text in the gateway.
2. **Anonymize before embedding.** Nothing is written to the vector table without passing through the Trust Engine.
3. **No external network calls at runtime.** Embeddings and generation go through Ollama only. No cloud APIs, no telemetry. (Pulling models during setup is the only exception.)
4. **Honest metrics.** Never hardcode or fabricate results. Every number shown in the dashboard or report must come from a real run and be reproducible from a script.
5. **Label simulations.** Anything injected or simulated (e.g., fault injection) must be labeled as such in data and UI.
6. **Tenant scoping.** Once Task 5 lands, every query touching documents must run with a tenant context; rely on Postgres row-level security, not only app-level filtering.
7. **Secrets in `.env`** only; keep `.env.example` updated; never commit `.env`.

## Conventions

- Backend code lives in `backend/app/`, organized by `trust_engine/`, `rag/`, `metrics/`, `db/`.
- Standalone evaluation scripts live in `scripts/` and write outputs to `reports/`.
- Config comes from environment variables via `app/config.py`. Inside Compose, reach services by name (`db`, `ollama`), not `localhost`.
- Tests in `backend/tests/`, named `test_<module>.py`. Add tests with every Trust Engine change, including cases that should NOT be flagged.
- Latency: use `time.perf_counter()`; record stages `pii_scan`, `retrieval`, `time_to_first_token`, `generation`, `total`, in milliseconds.

## Commands

```bash
docker compose up -d db ollama          # start infra
./scripts/pull_models.sh                # one-time model pull
docker compose up -d                    # start everything
docker compose run --rm backend pytest  # run tests
docker compose logs -f backend          # backend logs
```

## Current focus

Update this line as work progresses: **Tasks 1-4 and 6 done (gateway, per-stage latency, code-attached citations, canary audit; results so far measured on native Ollama). Next: Task 7 (dashboard + report), then Task 8 (packaging + air-gap proof); benchmark the all-Docker configuration for comparison.**
