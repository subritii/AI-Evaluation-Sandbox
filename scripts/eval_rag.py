"""Evaluate the policy RAG pipeline against a small labeled question set.

For each question this records:
  - retrieval: was the expected chunk in the top-k, and at what rank
  - answer:    does it contain the expected keywords (or, for out-of-scope
               questions, does it correctly refuse)
  - citations: do the code-attached citations include the chunk holding the
               evidence (and what share of them do); refusals must cite nothing
  - timings:   pii_scan, retrieval, time_to_first_token, generation, total (ms)

It calls `app.rag.pipeline.run_query`, the same function the API serves, so it
measures the real pipeline, not a copy of it. Refusal detection and citation
selection live in `app.rag.grounding`.

Run on the host, with db and ollama up and the policies ingested:
    .venv/bin/python scripts/eval_rag.py
    .venv/bin/python scripts/eval_rag.py --retrieve-only   # skip the LLM (fast)

Writes reports/eval_rag_<timestamp>.json with every answer and setting,
so each number in the summary can be traced back to a real run.
"""

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.config import get_settings  # noqa: E402
from app.db.connection import get_connection  # noqa: E402
from app.rag.ollama_client import OllamaClient  # noqa: E402
from app.rag.pipeline import run_query  # noqa: E402

REPORTS_DIR = REPO_ROOT / "reports"


@dataclass(frozen=True)
class EvalCase:
    """One labeled question.

    evidence: text found only in the chunk(s) that answer the question. Any
        alternative counts. Matching on text rather than chunk_index keeps the
        labels valid when chunking changes. Empty for out-of-scope questions.
    keywords: groups of alternatives; the answer must contain one alternative
        from EVERY group (case-insensitive), and must not also refuse: a hedged
        answer ("I don't know. The policy says...") counts as a failure.
    should_refuse: the policy doesn't cover this; the correct answer is a refusal.
    """

    id: str
    question: str
    evidence: tuple[str, ...] = ()
    keywords: tuple[tuple[str, ...], ...] = ()
    should_refuse: bool = False


# The first four are the questions tested by hand, in their original wording.
# The rest cover other sections, including a table row, a threshold that
# needs a comparison ($300,000 vs "$250,000 or more"), and a second refusal case.
EVAL_SET = [
    EvalCase(
        id="wire_callback",
        question="Which wire transfers need a callback?",
        evidence=("require a callback",),
        # The $10,000 phone/email/fax rule. (Changed wire instructions also need
        # a callback; not required here, so a partial answer still passes.)
        keywords=(("10,000",), ("phone", "email", "fax")),
    ),
    EvalCase(
        id="cvv_deletion",
        question="When must a CVV be deleted?",
        evidence=("Card verification values (CVV/CVC)",),
        # Policy: never stored after authorization.
        keywords=(("authoriz", "authoris"),),
    ),
    EvalCase(
        id="regulator_notification",
        question="Within how many hours must we notify our federal regulator of an incident?",
        evidence=("no later than 36 hours",),
        # Distractor in the same chunk: the CISO/CCO must be told within 4 hours.
        keywords=(("36 hours", "36-hour"),),
    ),
    EvalCase(
        id="dress_code",
        question="What is the bank's dress code?",
        should_refuse=True,
    ),
    EvalCase(
        id="kyc_retention",
        question="How long are KYC records kept?",
        evidence=("CIP/KYC",),
        keywords=(("5 years", "five years"), ("closed", "closure")),
    ),
    EvalCase(
        id="ctr_filing",
        question="When must a Currency Transaction Report be filed?",
        evidence=("within 15 calendar days",),
        keywords=(("15 calendar days", "15 days", "fifteen"),),
    ),
    EvalCase(
        id="wire_dual_control",
        question="What approval is needed for a $300,000 wire transfer?",
        evidence=("$250,000 or more",),
        keywords=(("two", "dual control", "2 authorized"),),
    ),
    EvalCase(
        id="breach_reporting",
        question="How quickly must an employee report a suspected data breach?",
        evidence=("within 1 hour of discovery",),
        keywords=(("1 hour", "one hour"),),
    ),
    EvalCase(
        id="access_recertification",
        question="How often must managers recertify their team's access to Restricted systems?",
        evidence=("every 90 days",),
        keywords=(("90 days",),),
    ),
    EvalCase(
        id="ai_log_retention",
        question="How long are AI assistant prompt and response logs retained?",
        # Stated in both the retention table (section 6) and the AI section (10).
        evidence=("AI assistant prompt and response logs", "retained for 1 year"),
        keywords=(("1 year", "one year"),),
    ),
    EvalCase(
        id="mortgage_rate",
        question="What is the current 30-year mortgage interest rate?",
        should_refuse=True,
    ),
]


@dataclass
class CaseResult:
    id: str
    question: str
    should_refuse: bool
    retrieved: list[dict]
    expected_rank: int | None  # 1-based rank of the first chunk containing evidence; None if absent
    retrieval_hit: bool | None  # None for refusal cases (no expected chunk)
    answer: str | None = None
    answer_pass: bool | None = None
    missing_keywords: list[list[str]] = field(default_factory=list)
    refused: bool | None = None
    citations: list[dict] = field(default_factory=list)
    # Answerable: a cited chunk contains the evidence. Refusal cases: nothing was cited.
    citation_pass: bool | None = None
    # Answerable only: share of cited chunks that contain the evidence.
    citation_precision: float | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)


def has_evidence(case: EvalCase, content: str) -> bool:
    return any(e.lower() in content.lower() for e in case.evidence)


def find_expected_rank(case: EvalCase, contents: list[str]) -> int | None:
    """Return the 1-based rank of the first retrieved chunk containing any evidence text."""
    for rank, content in enumerate(contents, start=1):
        if has_evidence(case, content):
            return rank
    return None


def missing_keyword_groups(case: EvalCase, answer: str) -> list[list[str]]:
    """Return the keyword groups with no alternative present in the answer."""
    lowered = answer.lower()
    return [list(group) for group in case.keywords if not any(k.lower() in lowered for k in group)]


def score_citations(case: EvalCase, result: CaseResult, citations, chunks) -> None:
    """Citation accuracy: are the code-attached citations the chunks holding the evidence?"""
    content = {(c.source, c.chunk_index): c.content for c in chunks}
    result.citations = [
        {"ref": f"{c.source}#{c.chunk_index}", "section": c.section, "support": c.support,
         "has_evidence": bool(case.evidence) and has_evidence(case, content[(c.source, c.chunk_index)])}
        for c in citations
    ]
    if case.should_refuse:
        result.citation_pass = not citations
    else:
        supported = sum(c["has_evidence"] for c in result.citations)
        result.citation_pass = supported > 0
        result.citation_precision = round(supported / len(citations), 3) if citations else None


def run_case(case: EvalCase, client: OllamaClient, conn, settings, top_k: int, retrieve_only: bool) -> CaseResult:
    """Run one question through the production pipeline and score it."""
    q = run_query(case.question, client=client, conn=conn, settings=settings, top_k=top_k, generate=not retrieve_only)

    rank = find_expected_rank(case, [c.content for c in q.chunks]) if case.evidence else None
    result = CaseResult(
        id=case.id,
        question=case.question,
        should_refuse=case.should_refuse,
        retrieved=[
            {"ref": f"{c.source}#{c.chunk_index}", "similarity": round(c.similarity, 4),
             "heading": c.content.split("\n", 1)[0]}
            for c in q.chunks
        ],
        expected_rank=rank,
        retrieval_hit=None if case.should_refuse else rank is not None,
        timings_ms=q.timings_ms,
    )

    if q.answer is not None:
        result.answer = q.answer
        result.refused = q.refused
        if case.should_refuse:
            result.answer_pass = q.refused
        else:
            result.missing_keywords = missing_keyword_groups(case, q.answer)
            # Keywords alone aren't enough: a compliance answer that hedges with
            # "I don't know" while quoting the rule is contradictory, so it fails.
            result.answer_pass = not result.missing_keywords and not q.refused
        score_citations(case, result, q.citations, q.chunks)
    return result


def _fmt_bool(value: bool | None) -> str:
    return "-" if value is None else ("PASS" if value else "FAIL")


def _fmt_ms(timings: dict[str, float], stage: str) -> str:
    # "-" for stages that didn't run, so a skipped stage never reads as 0 ms.
    return f"{timings[stage]:.0f}" if stage in timings else "-"


def print_summary(results: list[CaseResult], retrieve_only: bool) -> None:
    """Print a per-question table followed by aggregate scores."""
    header = (f"{'id':<24} {'type':<7} {'retr':<5} {'rank':<5} {'answer':<7} {'cite':<5} "
              f"{'retr_ms':>8} {'ttft_ms':>8} {'gen_ms':>8} {'total_ms':>9}")
    print("\n" + header)
    print("-" * len(header))
    for r in results:
        t = r.timings_ms
        print(
            f"{r.id:<24} {'refuse' if r.should_refuse else 'answer':<7} "
            f"{_fmt_bool(r.retrieval_hit):<5} {r.expected_rank or '-':<5} {_fmt_bool(r.answer_pass):<7} "
            f"{_fmt_bool(r.citation_pass):<5} "
            f"{_fmt_ms(t, 'retrieval'):>8} {_fmt_ms(t, 'time_to_first_token'):>8} "
            f"{_fmt_ms(t, 'generation'):>8} {_fmt_ms(t, 'total'):>9}"
        )
        if r.missing_keywords:
            print(f"{'':<24} missing keywords: {r.missing_keywords}")
        if r.refused and not r.should_refuse:
            print(f"{'':<24} answer hedged/refused although the policy covers it")

    answerable = [r for r in results if not r.should_refuse]
    hits = sum(bool(r.retrieval_hit) for r in answerable)
    top1 = sum(r.expected_rank == 1 for r in answerable)
    print(f"\nRetrieval hit@{len(results[0].retrieved) if results else 0}: {hits}/{len(answerable)}   top-1: {top1}/{len(answerable)}")
    if not retrieve_only:
        answered_ok = sum(bool(r.answer_pass) for r in answerable)
        refusals = [r for r in results if r.should_refuse]
        refused_ok = sum(bool(r.answer_pass) for r in refusals)
        print(f"Correct answers (keywords, no hedging): {answered_ok}/{len(answerable)}   correct refusals: {refused_ok}/{len(refusals)}")
        cite_hits = sum(bool(r.citation_pass) for r in answerable)
        precisions = [r.citation_precision for r in answerable if r.citation_precision is not None]
        mean_precision = f"{sum(precisions) / len(precisions):.0%}" if precisions else "-"
        clean_refusals = sum(bool(r.citation_pass) for r in refusals)
        print(f"Citations: evidence chunk cited {cite_hits}/{len(answerable)}   mean citation precision {mean_precision}"
              f"   refusals citing nothing {clean_refusals}/{len(refusals)}")


def _git_commit() -> str | None:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True).stdout
        return commit + ("-dirty" if dirty.strip() else "")
    except (OSError, subprocess.CalledProcessError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate retrieval and answers on a labeled policy question set.")
    parser.add_argument("-k", "--top-k", type=int, default=4, help="Chunks retrieved per question")
    parser.add_argument("--retrieve-only", action="store_true", help="Skip LLM generation")
    args = parser.parse_args()

    settings = get_settings()
    started_at = datetime.now(timezone.utc)
    load_before = os.getloadavg()
    client = OllamaClient(settings.ollama_base_url)
    results: list[CaseResult] = []
    try:
        with get_connection() as conn:
            chunk_count = conn.execute("SELECT count(*) FROM document_chunks").fetchone()[0]
            if chunk_count == 0:
                sys.exit("No chunks ingested. Run: docker compose run --rm backend python -m app.rag.ingest")
            for i, case in enumerate(EVAL_SET, start=1):
                print(f"[{i}/{len(EVAL_SET)}] {case.id} ...", flush=True)
                results.append(run_case(case, client, conn, settings, args.top_k, args.retrieve_only))
    finally:
        client.close()

    print_summary(results, args.retrieve_only)

    REPORTS_DIR.mkdir(exist_ok=True)
    out_path = REPORTS_DIR / f"eval_rag_{started_at.strftime('%Y%m%d-%H%M%S')}.json"
    report = {
        "started_at": started_at.isoformat(),
        "git_commit": _git_commit(),
        "config": {
            "llm_model": None if args.retrieve_only else settings.llm_model,
            "embed_model": settings.embed_model,
            # Container vs native Ollama changes latency a lot; record which one ran.
            "ollama_base_url": settings.ollama_base_url,
            "top_k": args.top_k,
            "chunk_size": settings.chunk_size,
            "chunk_overlap": settings.chunk_overlap,
            "chunks_in_db": chunk_count,
            "temperature": None if args.retrieve_only else settings.llm_temperature,
            "max_tokens": None if args.retrieve_only else settings.llm_max_tokens,
        },
        # Timings depend on what else the machine was doing; record it.
        "environment": {
            "host_load_avg_before": load_before,
            "host_load_avg_after": os.getloadavg(),
            "host_cpus": os.cpu_count(),
        },
        "results": [asdict(r) for r in results],
    }
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nSaved {out_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
