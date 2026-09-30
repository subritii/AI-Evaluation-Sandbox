"""Integration tests against a real Ollama server (skipped when none is reachable, e.g. in CI).

They check what the fakes elsewhere can't: that the configured models exist on
the server, the embedding size matches the vector column, and generation
streams through the real API.
"""

import pytest

from app.config import get_settings
from app.rag.ollama_client import OllamaClient

pytestmark = pytest.mark.requires_ollama


@pytest.fixture
def ollama():
    client = OllamaClient(get_settings().ollama_base_url)
    yield client
    client.close()


def test_embedding_model_returns_vectors_the_schema_accepts(ollama):
    settings = get_settings()
    [vector] = ollama.embed(settings.embed_model, ["search_query: How long are KYC records kept?"])
    assert len(vector) == settings.embed_dim


def test_llm_streams_a_short_answer(ollama):
    settings = get_settings()
    fragments = list(ollama.chat_stream(
        settings.llm_model, [{"role": "user", "content": "Reply with the single word: ready"}],
        temperature=0.0, max_tokens=5,
    ))
    assert "".join(fragments).strip()
