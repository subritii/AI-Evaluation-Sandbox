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
    "airgap_check": "airgap_check_",
}


def list_reports(reports_dir: Path, kind: str) -> list[Path]:
    """Report files of one kind, newest first (file names end in a sortable timestamp)."""
    return sorted(reports_dir.glob(f"{KINDS[kind]}*.json"), reverse=True)


# Kinds whose default is the run with the most latency samples, not the newest:
# the canary audit feeds the headline latency tiles, and a 20-request smoke run
# shouldn't displace a 200-request run just by being newer.
DEFAULT_BY_SAMPLES = {"canary_audit"}


def default_report(kind: str, files: list[Path]) -> Path | None:
    """The report the dashboard selects first: most latency samples for DEFAULT_BY_SAMPLES kinds
    (newest wins a tie; `files` is newest first), otherwise the newest."""
    if not files:
        return None
    if kind not in DEFAULT_BY_SAMPLES:
        return files[0]

    def samples(path: Path) -> int:
        try:
            return latency_n(load(path).get("latency"))
        except (OSError, ValueError):
            return 0

    return max(files, key=samples)  # max keeps the first (newest) of equal values


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
        # Runs inside the tools container measure the Docker VM; older reports don't say.
        "load_where": {"host": "host", "docker_vm": "Docker VM"}.get(
            (report.get("environment") or {}).get("load_measured_in"), "where not recorded"),
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
        # Full per-question records for the trace view. The questions are the
        # eval's own synthetic, labeled set, not user input.
        "cases": {r["id"]: _case(r) for r in results},
    }


def _case(r: dict) -> dict:
    return {
        "id": r["id"],
        "question": r.get("question", ""),
        "type": "refuse" if r["should_refuse"] else "answer",
        "expected_rank": r.get("expected_rank"),
        "retrieved": r.get("retrieved", []),
        "answer": r.get("answer"),
        "answer_pass": r.get("answer_pass"),
        "refused": r.get("refused"),
        "missing_keywords": r.get("missing_keywords", []),
        "citations": r.get("citations", []),
        "citation_pass": r.get("citation_pass"),
        "timings_ms": r.get("timings_ms") or {},
    }


def compare_evals(baseline: dict, current: dict) -> dict:
    """Per-question answer verdicts, baseline vs current (both from summarize_eval_rag)."""
    ids = list(current["cases"]) + [i for i in baseline["cases"] if i not in current["cases"]]
    rows = []
    for qid in ids:
        before = baseline["cases"].get(qid, {}).get("answer_pass")
        after = current["cases"].get(qid, {}).get("answer_pass")
        if qid not in baseline["cases"]:
            change = "new question"
        elif qid not in current["cases"]:
            change = "removed"
        elif before == after:
            change = "unchanged"
        else:
            change = "fixed" if after else "regressed"
        case = current["cases"].get(qid) or baseline["cases"][qid]
        rows.append({"id": qid, "type": case["type"], "baseline": before, "current": after, "change": change})
    return {
        "rows": rows,
        "baseline": {"answers": (baseline["answers_correct"], baseline["answerable"]),
                     "refusals": (baseline["refusals_correct"], baseline["refusal_cases"])},
        "current": {"answers": (current["answers_correct"], current["answerable"]),
                    "refusals": (current["refusals_correct"], current["refusal_cases"])},
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


def summarize_airgap(path: Path, report: dict) -> dict:
    """Air-gap check: verdict, which containers were isolated, and what was reported about the proxy."""
    checks = report.get("checks", {})
    inventory = checks.get("inventory", [])
    return {
        "source": _source(path, report),
        "verdict": report["verdict"],
        "model_backend": report.get("model_backend"),
        "problems": report.get("problems", []),
        "notes": report.get("notes", []),
        "isolated": sorted(c["service"] for c in inventory if c["isolated"]),
        "not_isolated": sorted(c["service"] for c in inventory if not c["isolated"]),
        "control_connected": all(
            str(v).startswith(("CONNECTED", "RESOLVED")) for v in checks.get("control_default_bridge", {}).values()
        ) and bool(checks.get("control_default_bridge")),
        "proxy_egress": checks.get("proxy_egress"),
        "functional_status": (checks.get("functional_query") or {}).get("status"),
        "inventory": inventory,
        "probes": {
            "in_container": checks.get("in_container", {}),
            "sandbox_network": checks.get("sandbox_network", {}),
            "control": checks.get("control_default_bridge", {}),
            "internal": checks.get("internal_reachability", {}),
            "ingress": checks.get("ingress", {}),
        },
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


def backend_label(backend: str | None) -> str:
    return {
        "native": "native Ollama (host, Apple GPU)",
        "docker": "Docker Ollama (container; CPU on macOS)",
        "remote": "remote model endpoint (another machine)",
        "unknown": "unknown (recorded before backends were tracked)",
        "mixed": "mixed backends",
        None: "not recorded",
    }.get(backend, backend)


def headline_latency(batch: dict | None, can: dict | None) -> tuple[dict, str] | None:
    """The latency summary with the most samples, and a label naming it.

    Headline percentiles should rest on the largest run available: a 20-question
    dashboard batch has P95 decided by one request, the 200-request canary batch by ten.
    """
    candidates = []
    if batch and latency_n(batch.get("latency")):
        backend = backend_label((batch.get("gateway") or {}).get("model_backend"))
        candidates.append((latency_n(batch["latency"]), batch["latency"], f"dashboard batch {batch['run_id']}, {backend}"))
    if can and latency_n(can.get("latency")):
        candidates.append((latency_n(can["latency"]), can["latency"], f"canary audit batch {can['run_id']}, {backend_label(backend_of(can))}"))
    if not candidates:
        return None
    n, latency, label = max(candidates, key=lambda c: c[0])  # ties keep the dashboard batch (listed first)
    return latency, f"n={n}, {label}"


def latency_runs(reports_dir: Path) -> list[dict]:
    """Every saved run with latency (canary audits and dashboard batches), newest first.

    Each item: file, kind, run_id, started_at, n, model_backend(s), providers,
    load at start, and the gateway's per-stage summary.
    """
    runs = []
    for kind in ("canary_audit", "dashboard_batch"):
        for path in list_reports(reports_dir, kind):
            try:
                raw = load(path)
            except (OSError, ValueError):
                continue
            latency = raw.get("latency") or {}
            if not latency_n(latency):
                continue
            models = latency.get("models") or raw.get("model_config") or []
            gateway = raw.get("gateway") or {}
            backends = sorted({m.get("model_backend") for m in models} or {gateway.get("model_backend")}, key=str)
            # Runs from before Task 9 recorded no provider: Ollama was the only one.
            providers = sorted({m.get(k, "ollama") for m in models for k in ("llm_provider", "embed_provider")}
                               or {gateway.get(k, "ollama") for k in ("llm_provider", "embed_provider")})
            env = raw.get("environment") or {}
            load_avg = env.get("host_load_avg_before") or env.get("gateway_load_avg_before")
            runs.append({
                "file": path.name, "kind": kind, "run_id": raw.get("run_id"), "started_at": raw.get("started_at"),
                "n": latency_n(latency), "model_backends": backends, "providers": providers,
                "load_at_start": load_avg[0] if load_avg else None, "latency": latency,
            })
    return sorted(runs, key=lambda r: r["file"].split("_")[-1], reverse=True)
