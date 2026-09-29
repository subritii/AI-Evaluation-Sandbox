"""Unit tests for loading, chunking, and embedding helpers (no DB or Ollama needed)."""

from pathlib import Path

import pytest

from app.rag.embeddings import DOCUMENT_PREFIX, QUERY_PREFIX, embed_documents, embed_query
from app.rag.ingest import chunk_documents, load_documents


class FakeOllama:
    """Stands in for OllamaClient; records inputs and returns fixed-size vectors."""

    def __init__(self, dim: int = 768):
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[0.1] * self.dim for _ in texts]


@pytest.fixture
def policies_dir(tmp_path: Path) -> Path:
    paragraphs = [f"Section {i}. " + ("Retention rule text. " * 20) for i in range(10)]
    (tmp_path / "policy.md").write_text("\n\n".join(paragraphs), encoding="utf-8")
    (tmp_path / "notes.docx").write_text("unsupported type, should be skipped")
    return tmp_path


def test_load_documents_skips_unsupported_files(policies_dir):
    docs = load_documents(policies_dir)
    assert [d.metadata["source"] for d in docs] == ["policy.md"]


def test_chunks_respect_size_and_are_numbered_per_source(policies_dir):
    chunks = chunk_documents(load_documents(policies_dir), chunk_size=500, chunk_overlap=50)
    assert len(chunks) > 1
    assert all(len(c.page_content) <= 500 for c in chunks)
    assert [c.metadata["chunk_index"] for c in chunks] == list(range(len(chunks)))


def test_real_policy_document_chunks():
    policies = Path(__file__).resolve().parents[2] / "data" / "policies"
    if not policies.exists():  # data/ is mounted at /data inside the container
        policies = Path("/data/policies")
    if not policies.exists():
        pytest.skip("policy directory not available")
    chunks = chunk_documents(load_documents(policies), chunk_size=1000, chunk_overlap=150)
    assert len(chunks) >= 10
    # The CVV rule must sit in a Payment Card Data chunk, not one shared with section 8.
    [cvv_chunk] = [c for c in chunks if "CVV" in c.page_content]
    assert cvv_chunk.metadata["section"] == "9. Payment Card Data"
    assert cvv_chunk.page_content.startswith("## 9. Payment Card Data\n\n")
    assert "## 8." not in cvv_chunk.page_content


@pytest.fixture
def markdown_dir(tmp_path: Path) -> Path:
    text = (
        "# Test Policy\n\nIntro paragraph.\n\n"
        "## 1. Short Section\n\nShort rule text.\n\n"
        "## 2. Long Section\n\n" + "\n\n".join(f"Long rule {i}. " + "detail " * 30 for i in range(6))
    )
    (tmp_path / "policy.md").write_text(text, encoding="utf-8")
    return tmp_path


def test_markdown_chunks_never_straddle_sections(markdown_dir):
    chunks = chunk_documents(load_documents(markdown_dir), chunk_size=400, chunk_overlap=40)
    for chunk in chunks:
        body = chunk.page_content.split("\n\n", 1)[1]
        assert "## " not in body  # no second heading inside a chunk
    assert chunks[0].page_content == "# Test Policy\n\nIntro paragraph."
    assert chunks[1].page_content == "## 1. Short Section\n\nShort rule text."
    assert chunks[1].metadata == {"source": "policy.md", "title": "Test Policy", "section": "1. Short Section", "chunk_index": 1}


def test_long_section_falls_back_to_size_split_with_prefix(markdown_dir):
    chunks = chunk_documents(load_documents(markdown_dir), chunk_size=400, chunk_overlap=40)
    long_chunks = [c for c in chunks if c.metadata.get("section") == "2. Long Section"]
    assert len(long_chunks) > 1
    assert all(c.page_content.startswith("## 2. Long Section\n\n") for c in long_chunks)
    assert all(len(c.page_content) <= 400 for c in chunks)  # prefix counts toward the limit


def test_nomic_task_prefixes_are_applied():
    client = FakeOllama()
    embed_documents(client, "m", ["a", "b"], expected_dim=768)
    embed_query(client, "m", "q", expected_dim=768)
    assert client.calls[0] == [DOCUMENT_PREFIX + "a", DOCUMENT_PREFIX + "b"]
    assert client.calls[1] == [QUERY_PREFIX + "q"]


def test_dimension_mismatch_is_rejected():
    with pytest.raises(ValueError, match="dimensions"):
        embed_query(FakeOllama(dim=384), "m", "q", expected_dim=768)
