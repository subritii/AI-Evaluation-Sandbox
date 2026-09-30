"""Embedding helpers for nomic-embed-text.

nomic-embed-text was trained with task prefixes: stored passages use
"search_document: " and questions use "search_query: ". Omitting them
degrades retrieval without raising any error, so they're applied here in one
place rather than at each call site.
"""

from app.rag.model_client import ChatEmbedClient

DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "

# Texts per /api/embed request. Keeps request size bounded for large ingests.
BATCH_SIZE = 32


def embed_documents(
    client: ChatEmbedClient, model: str, texts: list[str], expected_dim: int
) -> list[list[float]]:
    """Embed passages for storage, in batches."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = [DOCUMENT_PREFIX + t for t in texts[start : start + BATCH_SIZE]]
        vectors.extend(client.embed(model, batch))
    _check_dimensions(vectors, expected_dim)
    return vectors


def embed_query(client: ChatEmbedClient, model: str, question: str, expected_dim: int) -> list[float]:
    """Embed a user question for similarity search."""
    [vector] = client.embed(model, [QUERY_PREFIX + question])
    _check_dimensions([vector], expected_dim)
    return vector


def _check_dimensions(vectors: list[list[float]], expected_dim: int) -> None:
    # Catches a model swap that doesn't match the vector(N) column, with a
    # clearer message than the database error would give.
    for v in vectors:
        if len(v) != expected_dim:
            raise ValueError(
                f"Embedding has {len(v)} dimensions, expected {expected_dim}. "
                "Update EMBED_DIM and the vector column in db/schema.sql together."
            )
