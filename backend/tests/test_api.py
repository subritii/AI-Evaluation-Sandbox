"""Gateway tests: real Postgres (from Compose), fake Ollama, real Trust Engine.

Requires ingested policy chunks (docker compose run --rm backend python -m app.rag.ingest).
"""

import logging
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.connection import get_connection
from app.main import app, get_model_client

RAW_NAME, RAW_SSN = "Maria Lopez", "536-22-1847"


class FakeOllama:
    """Records what the model would have received; answers with a fixed policy sentence.

    Every query embeds to the stored vector of the Payment Card Data chunk, so
    retrieval ranks that chunk first, as the real model would for a CVV question.
    """

    def __init__(self, query_vector: list[float]):
        self.query_vector = query_vector
        self.embedded: list[str] = []
        self.prompts: list[str] = []

    def embed(self, model, texts):
        self.embedded.extend(texts)
        return [self.query_vector for _ in texts]

    def chat_stream(self, model, messages, temperature=None, max_tokens=None):
        self.prompts.append(messages[-1]["content"])
        yield "Card verification values must never be stored "
        yield "after authorization."


@pytest.fixture
def fake_ollama(client):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT embedding FROM document_chunks WHERE metadata->>'section' = '9. Payment Card Data' LIMIT 1"
        ).fetchone()
    if row is None:
        pytest.skip("Payment Card Data chunk not ingested")
    fake = FakeOllama(row[0].to_list())
    app.dependency_overrides[get_model_client] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    with get_connection() as conn:
        if not conn.execute("SELECT to_regclass('document_chunks')").fetchone()[0] or not conn.execute(
            "SELECT count(*) FROM document_chunks"
        ).fetchone()[0]:
            pytest.skip("no ingested chunks")
    with TestClient(app) as c:  # runs lifespan (schema + Trust Engine warmup)
        yield c


@pytest.fixture
def run_id():
    rid = f"test-{uuid.uuid4().hex[:8]}"
    yield rid
    with get_connection() as conn:
        conn.execute("DELETE FROM latency_samples WHERE run_id = %s", (rid,))
        conn.commit()


def test_query_masks_before_model_and_attaches_citations(client, fake_ollama, run_id, caplog):
    question = f"I'm {RAW_NAME}, SSN {RAW_SSN}. When must a CVV be deleted?"
    with caplog.at_level(logging.DEBUG):
        response = client.post("/query", json={"question": question, "run_id": run_id})
    assert response.status_code == 200
    body = response.json()

    # Masked before embedding, before the LLM, and in the response.
    assert body["masked_question"] == "I'm [PERSON_1], SSN [US_SSN_1]. When must a CVV be deleted?"
    assert body["masked_entities"] == {"PERSON": 1, "US_SSN": 1}
    for raw in (RAW_NAME, RAW_SSN):
        assert all(raw not in t for t in fake_ollama.embedded + fake_ollama.prompts)
        assert raw not in response.text
        assert raw not in caplog.text

    # Citations come from code, point at retrieved chunks, and favor the CVV rule.
    assert body["refused"] is False
    assert body["citations"] and all(c["source"] and c["chunk_index"] >= 0 for c in body["citations"])
    assert "Payment Card Data" in (body["citations"][0]["section"] or "")
    assert set(body["timings_ms"]) == {"pii_scan", "retrieval", "time_to_first_token", "generation", "total"}


def test_latency_samples_stored_and_summarized(client, fake_ollama, run_id):
    for _ in range(3):
        assert client.post("/query", json={"question": "How long are KYC records kept?", "run_id": run_id}).status_code == 200

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT stage, count(*) FROM latency_samples WHERE run_id = %s GROUP BY stage", (run_id,)
        ).fetchall()
    assert dict(rows) == {s: 3 for s in ("pii_scan", "retrieval", "time_to_first_token", "generation", "total")}

    metrics = client.get(f"/metrics/{run_id}").json()
    assert metrics["requests"] == 3
    total = metrics["stages"]["total"]
    assert total["n"] == 3 and total["p50"] <= total["p95"] <= total["p99"] <= total["max"]
    assert any("not reliable" in note for note in metrics["notes"])  # 3 samples is too few for P99


def test_latency_samples_record_model_config(client, fake_ollama, run_id):
    """Every sample names the backend and models, so no percentile is quoted without its configuration."""
    assert client.post("/query", json={"question": "How long are KYC records kept?", "run_id": run_id}).status_code == 200
    settings = get_settings()
    expected = (settings.model_backend, settings.llm_model, settings.embed_model,
                settings.llm_provider, settings.embed_provider)

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT model_backend, llm_model, embed_model, llm_provider, embed_provider"
            " FROM latency_samples WHERE run_id = %s",
            (run_id,),
        ).fetchall()
    assert rows == [expected]
    # Compose sets MODEL_BACKEND for the backend service; it should never fall back to "unknown" there.
    assert settings.model_backend in ("docker", "native")

    models = client.get(f"/metrics/{run_id}").json()["models"]
    keys = ("model_backend", "llm_model", "embed_model", "llm_provider", "embed_provider")
    assert models == [dict(zip(keys, expected), requests=1)]


def test_info_reports_config_without_secrets(client):
    info = client.get("/info").json()
    settings = get_settings()
    assert info["model_backend"] == settings.model_backend and info["llm_model"] == settings.llm_model
    assert info["scrubber"].startswith("presidio-") and len(info["load_avg"]) == 3
    assert info["llm_provider"] == settings.llm_provider and info["embed_provider"] == settings.embed_provider
    assert info["llm_endpoint"] and info["embed_endpoint"]
    assert settings.postgres_password not in str(info)
    if settings.openai_api_key and settings.openai_api_key.get_secret_value():
        assert settings.openai_api_key.get_secret_value() not in str(info)


def test_metrics_unknown_run_is_404(client):
    assert client.get("/metrics/no-such-run").status_code == 404


def test_validation_error_does_not_echo_input(client):
    question = f"SSN {RAW_SSN} " + "x" * 5000  # over the 4000-char limit
    response = client.post("/query", json={"question": question})
    assert response.status_code == 422
    assert RAW_SSN not in response.text
