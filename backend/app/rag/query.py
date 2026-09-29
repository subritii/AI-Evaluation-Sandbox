"""Ask a question against the ingested policies (retrieve + generate).

Run inside Compose:
    docker compose run --rm backend python -m app.rag.query "How long are KYC records kept?"
    docker compose run --rm backend python -m app.rag.query --retrieve-only "..."

The question is never logged; only the answer, retrieved policy excerpts, and
timings are printed. (No Trust Engine on the question yet; that's Task 2/4.)
"""

import argparse
import sys
import time

from app.config import get_settings
from app.db.connection import get_connection
from app.rag.embeddings import embed_query
from app.rag.ollama_client import OllamaClient
from app.rag.retrieval import RetrievedChunk, retrieve

SYSTEM_PROMPT = (
    "You are a compliance assistant for Cobalt Harbor Bank. Answer the question "
    "using ONLY the policy excerpts provided. Cite the excerpts you used like "
    "[source#chunk]. If the excerpts do not contain the answer, say you don't "
    "know rather than guessing."
)


def build_messages(question: str, chunks: list[RetrievedChunk]) -> list[dict]:
    """Assemble chat messages with the retrieved excerpts as grounding context."""
    context = "\n\n".join(f"[{c.source}#{c.chunk_index}]\n{c.content}" for c in chunks)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Policy excerpts:\n\n{context}\n\nQuestion: {question}"},
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the policy RAG pipeline.")
    parser.add_argument("question", help="Question to ask about the policies")
    parser.add_argument("-k", "--top-k", type=int, default=4, help="Number of chunks to retrieve")
    parser.add_argument("--retrieve-only", action="store_true", help="Skip LLM generation")
    args = parser.parse_args()

    settings = get_settings()
    client = OllamaClient(settings.ollama_base_url)
    timings: dict[str, float] = {}
    t_start = time.perf_counter()

    try:
        # Stage: retrieval (query embedding + vector search).
        query_vector = embed_query(client, settings.embed_model, args.question, settings.embed_dim)
        with get_connection() as conn:
            chunks = retrieve(conn, query_vector, args.top_k)
        timings["retrieval"] = (time.perf_counter() - t_start) * 1000

        if not chunks:
            sys.exit("No documents found. Run: docker compose run --rm backend python -m app.rag.ingest")

        print("Retrieved chunks:")
        for c in chunks:
            preview = " ".join(c.content.split())[:110]
            print(f"  [{c.source}#{c.chunk_index}] similarity={c.similarity:.3f}  {preview}...")

        if not args.retrieve_only:
            # Stages: time_to_first_token and generation (first token -> last token).
            print(f"\nAnswer ({settings.llm_model}):\n")
            t_gen_start = time.perf_counter()
            t_first = None
            for fragment in client.chat_stream(
                settings.llm_model, build_messages(args.question, chunks), temperature=settings.llm_temperature
            ):
                if t_first is None:
                    t_first = time.perf_counter()
                print(fragment, end="", flush=True)
            t_gen_end = time.perf_counter()
            print()
            if t_first is not None:
                timings["time_to_first_token"] = (t_first - t_gen_start) * 1000
                timings["generation"] = (t_gen_end - t_first) * 1000
    finally:
        client.close()

    timings["total"] = (time.perf_counter() - t_start) * 1000
    print("\nTimings (ms): " + "  ".join(f"{k}={v:.0f}" for k, v in timings.items()))


if __name__ == "__main__":
    main()
