"""FastAPI gateway: POST /query and GET /metrics/{run_id}.

Run (Compose starts this by default):
    uvicorn app.main:app --host 0.0.0.0 --port 8000

Logging rule: request and response bodies are never logged. The app logs a
request ID, masked-entity counts, and timings only. uvicorn's access log
records method, path, and status, never bodies.
"""

import logging
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import asdict

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.db.connection import get_connection, init_schema
from app.metrics.latency import ModelConfig, record_samples, run_metrics
from app.rag.ollama_client import OllamaClient, OllamaError
from app.rag.pipeline import run_query
from app.trust_engine import scrub

# uvicorn configures only its own loggers; give ours a handler too.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("app.gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables and load the Trust Engine model before accepting traffic,
    so the first request's pii_scan isn't a multi-second model load."""
    with get_connection() as conn:
        init_schema(conn)
    scrub("warm up")
    logger.info("Gateway ready (Trust Engine loaded)")
    yield


app = FastAPI(title="Secure AI Evaluation Sandbox Gateway", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's default 422 body echoes the submitted input, which may hold raw PII.
    Return only where and why validation failed."""
    errors = [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


# --- Dependencies (overridable in tests) ---

def get_db() -> Iterator[psycopg.Connection]:
    with get_connection() as conn:
        yield conn


def get_ollama() -> Iterator[OllamaClient]:
    client = OllamaClient(get_settings().ollama_base_url)
    try:
        yield client
    finally:
        client.close()


# --- Schemas ---

class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=4, ge=1, le=20)
    # Groups requests for GET /metrics/{run_id}; batches pass their own.
    run_id: str = Field(default="adhoc", min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")


class CitationOut(BaseModel):
    source: str
    chunk_index: int
    section: str | None
    similarity: float
    support: float


class QueryResponse(BaseModel):
    request_id: uuid.UUID
    run_id: str
    # The question exactly as the embedding model and LLM saw it (after masking).
    masked_question: str
    masked_entities: dict[str, int]
    answer: str
    refused: bool
    # Attached by code from the retrieved chunks; the model never writes these.
    citations: list[CitationOut]
    timings_ms: dict[str, float]


# --- Routes ---

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/query", response_model=QueryResponse)
def query(
    body: QueryRequest,
    conn: psycopg.Connection = Depends(get_db),
    client: OllamaClient = Depends(get_ollama),
) -> QueryResponse:
    """Scrub the question, retrieve policy chunks, generate an answer, attach citations.

    Per-stage timings are stored in latency_samples under `run_id`.
    """
    request_id = uuid.uuid4()
    settings = get_settings()
    try:
        result = run_query(body.question, client=client, conn=conn, settings=settings, top_k=body.top_k)
    except OllamaError as exc:
        # Ollama's error text is about the model/server, not the request; still, log the type only.
        logger.error("request %s failed: %s", request_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Model server error") from exc

    if not result.chunks or result.answer is None:
        raise HTTPException(status_code=503, detail="No documents ingested")

    model = ModelConfig(settings.model_backend, settings.llm_model, settings.embed_model)
    record_samples(conn, body.run_id, request_id, result.timings_ms, model)
    logger.info(
        "request %s run=%s masked=%s refused=%s citations=%d total_ms=%.0f",
        request_id, body.run_id, result.masked_entities or "none", result.refused,
        len(result.citations), result.timings_ms["total"],
    )
    return QueryResponse(
        request_id=request_id,
        run_id=body.run_id,
        masked_question=result.masked_question,
        masked_entities=result.masked_entities,
        answer=result.answer,
        refused=result.refused,
        citations=[CitationOut(**asdict(c)) for c in result.citations],
        timings_ms=result.timings_ms,
    )


@app.get("/metrics/{run_id}")
def metrics(run_id: str, conn: psycopg.Connection = Depends(get_db)) -> dict:
    """avg, P50, P90, P95, P99 (ms) per stage for one run."""
    summary = run_metrics(conn, run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"No latency samples for run_id '{run_id}'")
    return summary
