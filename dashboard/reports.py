"""Find saved evaluation reports and reduce each to what the dashboard and HTML report show.

Every summary carries its provenance (file, start time, git commit, model
backend), so no number is shown without the run that produced it. Fields that
older reports don't have are reported as None ("not recorded"), never guessed.
"""

import json
from pathlib import Path

# File prefix in reports/ for each kind of run.
KINDS = {
    "dashboard_batch": "dashboard_batch_",
    "eval_rag": "eval_rag_",
    "eval_detector": "eval_detector_",
    "canary_audit": "canary_audit_",
}


def list_reports(reports_dir: Path, kind: str) -> list[Path]:
    """Report files of one kind, newest first (file names end in a sortable timestamp)."""
    return sorted(reports_dir.glob(f"{KINDS[kind]}*.json"), reverse=True)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _source(path: Path, report: dict) -> dict:
    return {"file": path.name, "started_at": report.get("started_at"), "git_commit": report.get("git_commit")}


def summarize_eval_rag(path: Path, report: dict) -> dict:
    """RAG eval: retrieval, answer, and citation pass counts, plus per-question rows."""
    results = report["results"]
    answerable = [r for r in results if not r["should_refuse"]]
    refusals = [r for r in results if r["should_refuse"]]
    retrieve_only = report["config"].get("llm_model") is None
    precisions = [r["citation_precision"] for r in answerable if r.get("citation_precision") is not None]
    return {
        "source": _source(path, report),
        "model_backend": report["config"].get("model_backend"),
        "llm_model": report["config"].get("llm_model"),
        "embed_model": report["config"].get("embed_model"),
        "retrieve_only": retrieve_only,
        "host_load_avg": [
            (report.get("environment") or {}).get("host_load_avg_before"),
            (report.get("environment") or {}).get("host_load_avg_after"),
        ],
        "questions": len(results),
        "retrieval_hits": sum(bool(r["retrieval_hit"]) for r in answerable),
        "top1": sum(r.get("expected_rank") == 1 for r in answerable),
        "answerable": len(answerable),
        "answers_correct": None if retrieve_only else sum(bool(r["answer_pass"]) for r in answerable),
        "refusals_correct": None if retrieve_only else sum(bool(r["answer_pass"]) for r in refusals),
        "refusal_cases": len(refusals),
        # Older reports predate code-attached citations.
        "citations_hit": sum(bool(r.get("citation_pass")) for r in answerable) if "citation_pass" in results[0] else None,
        "citation_precision": sum(precisions) / len(precisions) if precisions else None,
        "rows": [
            {
                "id": r["id"],
                "type": "refuse" if r["should_refuse"] else "answer",
                "retrieval": None if r["should_refuse"] else bool(r["retrieval_hit"]),
                "answer": r.get("answer_pass"),
                "citation": r.get("citation_pass"),
                "total_ms": (r.get("timings_ms") or {}).get("total"),
            }
            for r in results
        ],
    }


def summarize_detector(path: Path, report: dict) -> dict:
    """Detector eval: per-entity precision/recall and the standard vs hard split."""
    return {
        "source": _source(path, report),
        "scrubber": report.get("scrubber"),
        "threshold": report.get("score_threshold"),
        "dataset": report.get("dataset"),
        "per_entity": report["per_entity"],
        "overall": report["overall_micro"],
        "by_difficulty": report.get("by_difficulty", {}),
        "negatives": report.get("negative_records_flagged"),
        "latency": report.get("latency"),
    }


# Where a found canary means the Trust Engine missed it: the text sent to the models.
UNMASKED_LOCATION = "response.masked_question"


def split_findings(findings: list[dict]) -> dict:
    """Distinct canaries (request, value) in two groups, since they mean different things:

    unmasked:   in the masked question, so the Trust Engine missed it and the
                embedding model and LLM received it (a detector miss).
    downstream: in an answer, another response field, a log, or the database,
                i.e. it ended up somewhere it persists or is shown (a leak past the models).
    A canary can be in both.
    """
    groups: dict[str, dict] = {"unmasked": {}, "downstream": {}}
    for f in findings:
        group = "unmasked" if f["location"] == UNMASKED_LOCATION else "downstream"
        groups[group][(f["request"], f["value_synthetic"])] = f["entity_type"]
    result = {}
    for group, canaries in groups.items():
        by_entity: dict[str, int] = {}
        for entity in canaries.values():
            by_entity[entity] = by_entity.get(entity, 0) + 1
        result[group] = {"total": len(canaries), "by_entity": by_entity}
    return result


def summarize_canary(path: Path, report: dict) -> dict:
    """Canary audit: canaries found vs planted, by entity and location, and what wasn't searched."""
    models = report.get("model_config") or (report.get("latency") or {}).get("models")
    return {
        "source": _source(path, report),
        "run_id": report.get("run_id"),
        "models": models or None,
        "requests": report["config"]["requests"],
        "host_load_avg": [
            report["environment"].get("host_load_avg_before"),
            report["environment"].get("host_load_avg_after"),
        ],
        "planted": report["planted"],
        "found": report["found"],
        **split_findings(report.get("findings", [])),
        "searched": report.get("searched_locations"),
        # Reports from before this field existed didn't check for gaps.
        "not_searched": report.get("not_searched"),
        "latency": report.get("latency"),
    }


def latency_n(latency: dict | None) -> int:
    """Samples behind a latency summary (the `total` stage), 0 if none."""
    return (((latency or {}).get("stages") or {}).get("total") or {}).get("n", 0)


def backend_of(summary: dict | None) -> str | None:
    """The model backend a summary ran on, if recorded ("docker", "native", or "unknown")."""
    if not summary:
        return None
    if summary.get("model_backend"):
        return summary["model_backend"]
    models = summary.get("models") or []
    backends = {m["model_backend"] for m in models}
    return backends.pop() if len(backends) == 1 else ("mixed" if backends else None)
