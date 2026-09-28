"""Ingest policy documents into pgvector.

Pipeline per file:  load -> chunk -> Trust Engine scrub -> embed -> store

Run inside Compose:
    docker compose run --rm backend python -m app.rag.ingest

Re-running is safe: each file's existing chunks are replaced in a single
transaction, so a re-ingest never leaves a half-updated document behind.
"""

import argparse
import logging
import time
from pathlib import Path

import numpy as np
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from psycopg.types.json import Jsonb
from pypdf import PdfReader

from app.config import get_settings
from app.db.connection import get_connection, init_schema
from app.rag.embeddings import embed_documents
from app.rag.ollama_client import OllamaClient
from app.trust_engine import scrub_for_storage

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {".md", ".txt", ".pdf"}


def load_documents(policies_dir: Path) -> list[Document]:
    """Load every supported file in `policies_dir`, tagging each with its source name.

    PDFs load as one Document per page; text/Markdown as one Document per file.
    We read files directly (pypdf for PDFs) instead of using the
    langchain-community loaders, which are deprecated and unmaintained.
    """
    documents: list[Document] = []
    for path in sorted(policies_dir.iterdir()):
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            continue
        # Source is the file name only, not the (container) path.
        if suffix == ".pdf":
            for page_number, page in enumerate(PdfReader(path).pages, start=1):
                text = page.extract_text() or ""
                if text.strip():
                    documents.append(Document(page_content=text, metadata={"source": path.name, "page": page_number}))
        else:
            documents.append(Document(page_content=path.read_text(encoding="utf-8"), metadata={"source": path.name}))
    return documents


def chunk_documents(documents: list[Document], chunk_size: int, chunk_overlap: int) -> list[Document]:
    """Split documents into overlapping chunks, numbering chunks per source file.

    RecursiveCharacterTextSplitter tries paragraph breaks first, then lines,
    then words, so chunks follow the document's structure where possible.
    """
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    chunks = splitter.split_documents(documents)

    counters: dict[str, int] = {}
    for chunk in chunks:
        source = chunk.metadata["source"]
        chunk.metadata["chunk_index"] = counters.get(source, 0)
        counters[source] = chunk.metadata["chunk_index"] + 1
    return chunks


def store_chunks(conn, chunks: list[Document], vectors: list[list[float]], scrubbed_by: list[str], embed_model: str) -> None:
    """Replace each source's rows with the new chunks, one transaction per source."""
    by_source: dict[str, list[int]] = {}
    for i, chunk in enumerate(chunks):
        by_source.setdefault(chunk.metadata["source"], []).append(i)

    for source, indices in by_source.items():
        with conn.transaction():
            conn.execute("DELETE FROM document_chunks WHERE source = %s", (source,))
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO document_chunks
                        (source, chunk_index, content, metadata, embedding, embed_model, scrubbed_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            source,
                            chunks[i].metadata["chunk_index"],
                            chunks[i].page_content,
                            _json_metadata(chunks[i].metadata),
                            np.array(vectors[i], dtype=np.float32),
                            embed_model,
                            scrubbed_by[i],
                        )
                        for i in indices
                    ],
                )


def _json_metadata(metadata: dict) -> Jsonb:
    # source/chunk_index already have their own columns.
    return Jsonb({k: v for k, v in metadata.items() if k not in ("source", "chunk_index")})


def ingest(policies_dir: Path) -> None:
    """Run the full ingest pipeline and log a summary (counts and timings only)."""
    settings = get_settings()
    t0 = time.perf_counter()

    documents = load_documents(policies_dir)
    if not documents:
        raise SystemExit(f"No {sorted(SUPPORTED_SUFFIXES)} files found in {policies_dir}")
    chunks = chunk_documents(documents, settings.chunk_size, settings.chunk_overlap)

    # Anonymize before embedding: the scrubbed text is what gets embedded AND stored.
    results = [scrub_for_storage(c.page_content) for c in chunks]
    for chunk, result in zip(chunks, results):
        chunk.page_content = result.text
    t_scrub = time.perf_counter()

    client = OllamaClient(settings.ollama_base_url)
    try:
        vectors = embed_documents(client, settings.embed_model, [c.page_content for c in chunks], settings.embed_dim)
    finally:
        client.close()
    t_embed = time.perf_counter()

    with get_connection() as conn:
        init_schema(conn)
        store_chunks(conn, chunks, vectors, [r.scrubbed_by for r in results], settings.embed_model)
    t_store = time.perf_counter()

    sources = sorted({c.metadata["source"] for c in chunks})
    logger.info("Ingested %d chunks from %d file(s): %s", len(chunks), len(sources), ", ".join(sources))
    logger.info(
        "Timings (ms): load+chunk+scrub=%.0f embed=%.0f store=%.0f total=%.0f",
        (t_scrub - t0) * 1000, (t_embed - t_scrub) * 1000, (t_store - t_embed) * 1000, (t_store - t0) * 1000,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Ingest policy documents into pgvector.")
    parser.add_argument("--dir", type=Path, default=None, help="Directory of policy files (default: POLICIES_DIR)")
    args = parser.parse_args()
    ingest(args.dir or get_settings().policies_dir)


if __name__ == "__main__":
    main()
