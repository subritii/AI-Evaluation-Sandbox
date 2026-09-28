"""Similarity search over the document_chunks table."""

from dataclasses import dataclass

import numpy as np
import psycopg


@dataclass(frozen=True)
class RetrievedChunk:
    source: str
    chunk_index: int
    content: str
    # 1 - cosine distance. 1.0 = same direction; ~0 = unrelated.
    similarity: float


def retrieve(conn: psycopg.Connection, query_vector: list[float], top_k: int = 4) -> list[RetrievedChunk]:
    """Return the `top_k` chunks closest to `query_vector` by cosine distance.

    `<=>` is pgvector's cosine-distance operator; ordering by it lets Postgres
    use the HNSW index. (Task 5 adds tenant scoping via row-level security.)
    """
    rows = conn.execute(
        """
        SELECT source, chunk_index, content, embedding <=> %(q)s AS distance
        FROM document_chunks
        ORDER BY embedding <=> %(q)s
        LIMIT %(k)s
        """,
        {"q": np.array(query_vector, dtype=np.float32), "k": top_k},
    ).fetchall()
    return [
        RetrievedChunk(source=r[0], chunk_index=r[1], content=r[2], similarity=1.0 - float(r[3]))
        for r in rows
    ]
