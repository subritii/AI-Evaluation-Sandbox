"""Shared test setup: the `requires_ollama` marker and policy chunks for API tests.

No test needs a GPU. Only tests marked `requires_ollama` call a real model
server; they are skipped when none is reachable (as in CI, which has no Ollama).
"""

import hashlib
from pathlib import Path

import httpx
import pytest

from app.config import get_settings

# Stored as document_chunks.embed_model for chunks seeded by the tests, so
# they are recognizable and removed afterwards.
FAKE_EMBED_MODEL = "test-fake-embedder"


def pytest_configure(config):
    config.addinivalue_line("markers", "requires_ollama: calls a real Ollama server; skipped when none is reachable")


def _ollama_reachable(url: str) -> bool:
    try:
        return httpx.get(f"{url}/api/version", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def pytest_collection_modifyitems(config, items):
    marked = [item for item in items if "requires_ollama" in item.keywords]
    if not marked:
        return
    url = get_settings().ollama_base_url
    if _ollama_reachable(url):
        return
    skip = pytest.mark.skip(reason=f"requires_ollama: no Ollama reachable at {url} (e.g. CI)")
    for item in marked:
        item.add_marker(skip)


class HashEmbedder:
    """Deterministic 768-dim vectors from text hashes: stands in for the embedding model."""

    def __init__(self, dim: int):
        self.dim = dim

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            seed = hashlib.sha256(text.encode()).digest()
            raw = (seed * (self.dim // len(seed) + 1))[: self.dim]
            vectors.append([b / 255.0 - 0.5 for b in raw])
        return vectors


def _policies_dir() -> Path:
    for candidate in (Path(__file__).resolve().parents[2] / "data" / "policies", Path("/data/policies")):
        if candidate.exists():
            return candidate
    pytest.skip("policy directory not available")


@pytest.fixture(scope="session")
def policy_chunks():
    """Make sure document_chunks holds the policy.

    With a real ingest already in the DB (local stack), use it untouched.
    Otherwise (CI: empty database, no model server) run the real ingest
    pipeline (load, chunk, Trust Engine, store) with a fake embedder, and
    delete those rows at the end so no fake vectors outlive the tests.
    """
    from app.db.connection import get_connection, init_schema
    from app.rag.ingest import ingest

    with get_connection() as conn:
        init_schema(conn)
        existing = conn.execute("SELECT count(*) FROM document_chunks").fetchone()[0]
    if existing:
        yield "existing"
        return

    ingest(_policies_dir(), client=HashEmbedder(get_settings().embed_dim), embed_model=FAKE_EMBED_MODEL)
    yield "seeded"
    with get_connection() as conn:
        conn.execute("DELETE FROM document_chunks WHERE embed_model = %s", (FAKE_EMBED_MODEL,))
        conn.commit()
