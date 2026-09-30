"""The query pipeline: scrub -> retrieve -> generate -> attach citations, timed per stage.

One function serves the FastAPI gateway, the CLI (app.rag.query), and the
evaluation script, so the eval measures exactly what the API runs.

Stage timings (milliseconds, time.perf_counter):
    pii_scan             Trust Engine on the question
    retrieval            query embedding + vector search
    time_to_first_token  request sent -> first generated token
    generation           first token -> last token
    total                start -> citations attached
"""

import time
from dataclasses import dataclass, field

import psycopg

from app.config import Settings
from app.rag.embeddings import embed_query
from app.rag.grounding import Citation, is_refusal, select_citations
from app.rag.model_client import ChatEmbedClient
from app.rag.retrieval import RetrievedChunk, retrieve
from app.trust_engine import scrub

SYSTEM_PROMPT = (
    "You are a compliance assistant for Cobalt Harbor Bank. Answer the question "
    "using ONLY the policy excerpts provided. Do not add citations or mention "
    "excerpt numbers; sources are attached automatically. If the excerpts do not "
    "contain the answer, say you don't know rather than guessing. Text in square "
    "brackets such as [PERSON_1] is masked personal data: refer to it as written "
    "and never guess its value."
)


@dataclass
class QueryResult:
    """Everything the pipeline produced. Holds only the MASKED question."""

    masked_question: str
    masked_entities: dict[str, int]
    chunks: list[RetrievedChunk]
    answer: str | None = None
    refused: bool | None = None
    citations: list[Citation] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)


def build_messages(question: str, chunks: list[RetrievedChunk]) -> list[dict]:
    """Assemble chat messages with the retrieved excerpts as grounding context.

    Excerpts carry their section heading (the chunk's first line) but no IDs,
    so there is nothing for the model to copy as a fake citation.
    """
    context = "\n\n---\n\n".join(c.content for c in chunks)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Policy excerpts:\n\n{context}\n\nQuestion: {question}"},
    ]


def run_query(
    question: str,
    *,
    client: ChatEmbedClient,
    conn: psycopg.Connection,
    settings: Settings,
    top_k: int = 4,
    generate: bool = True,
) -> QueryResult:
    """Run the full pipeline on one question. The raw question never leaves this function."""
    timings: dict[str, float] = {}
    t_start = time.perf_counter()

    # Stage: pii_scan. Everything downstream (embedding, LLM, response) sees only the masked text.
    scrubbed = scrub(question)
    t_scanned = time.perf_counter()
    timings["pii_scan"] = (t_scanned - t_start) * 1000

    # Stage: retrieval.
    query_vector = embed_query(client, settings.embed_model, scrubbed.text, settings.embed_dim)
    chunks = retrieve(conn, query_vector, top_k)
    timings["retrieval"] = (time.perf_counter() - t_scanned) * 1000

    result = QueryResult(masked_question=scrubbed.text, masked_entities=scrubbed.entity_counts, chunks=chunks)

    if generate and chunks:
        # Stages: time_to_first_token and generation.
        t_gen_start = time.perf_counter()
        t_first = None
        fragments: list[str] = []
        for fragment in client.chat_stream(
            settings.llm_model,
            build_messages(scrubbed.text, chunks),
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        ):
            if t_first is None:
                t_first = time.perf_counter()
            fragments.append(fragment)
        t_gen_end = time.perf_counter()
        if t_first is not None:
            timings["time_to_first_token"] = (t_first - t_gen_start) * 1000
            timings["generation"] = (t_gen_end - t_first) * 1000

        result.answer = "".join(fragments).strip()
        result.refused = is_refusal(result.answer)
        result.citations = select_citations(result.answer, chunks, result.refused)

    timings["total"] = (time.perf_counter() - t_start) * 1000
    result.timings_ms = {k: round(v, 2) for k, v in timings.items()}
    return result
