"""Ask a question against the ingested policies from the command line.

Run inside Compose:
    docker compose run --rm backend python -m app.rag.query "How long are KYC records kept?"
    docker compose run --rm backend python -m app.rag.query --retrieve-only "..."

Uses the same pipeline as the API (app.rag.pipeline). Prints the MASKED
question, never the raw one, plus the answer, code-attached sources, and timings.
"""

import argparse
import sys

from app.config import get_settings
from app.db.connection import get_connection
from app.rag.ollama_client import OllamaClient
from app.rag.pipeline import run_query


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the policy RAG pipeline.")
    parser.add_argument("question", help="Question to ask about the policies")
    parser.add_argument("-k", "--top-k", type=int, default=4, help="Number of chunks to retrieve")
    parser.add_argument("--retrieve-only", action="store_true", help="Skip LLM generation")
    args = parser.parse_args()

    settings = get_settings()
    client = OllamaClient(settings.ollama_base_url)
    try:
        with get_connection() as conn:
            result = run_query(
                args.question, client=client, conn=conn, settings=settings,
                top_k=args.top_k, generate=not args.retrieve_only,
            )
    finally:
        client.close()

    if not result.chunks:
        sys.exit("No documents found. Run: docker compose run --rm backend python -m app.rag.ingest")

    print(f"Question after Trust Engine: {result.masked_question}")
    print("\nRetrieved chunks:")
    for c in result.chunks:
        preview = " ".join(c.content.split())[:110]
        print(f"  [{c.source}#{c.chunk_index}] similarity={c.similarity:.3f}  {preview}...")

    if result.answer is not None:
        print(f"\nAnswer ({settings.llm_model}):\n\n{result.answer}\n")
        print("Sources (attached by code):" if result.citations else "Sources: none (answer refused or ungrounded)")
        for c in result.citations:
            print(f"  [{c.source}#{c.chunk_index}] {c.section or ''}  support={c.support}")

    print("\nTimings (ms): " + "  ".join(f"{k}={v:.0f}" for k, v in result.timings_ms.items()))


if __name__ == "__main__":
    main()
