"""Parse an uploaded question batch, send it through the gateway, and save the run.

The dashboard uses these functions; they also run from the command line, so any
number the dashboard shows can be reproduced by a script (rule: honest metrics):

    docker compose run --rm dashboard python batch.py /data/sample_batches/policy_questions.jsonl

Privacy: uploaded questions may hold raw PII. They stay in memory, are never
logged, and are never written to disk. Only what the gateway returns after the
Trust Engine is kept: entity counts and timings on disk; the masked question
only in memory for display, since masking can miss PII.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

STAGES = ("pii_scan", "retrieval", "time_to_first_token", "generation", "total")
# Same limit as the gateway's QueryRequest.question.
MAX_QUESTION_CHARS = 4000
# Sequential requests at ~2 s each (native Ollama) or ~30 s (Docker CPU):
# 1,000 is already a long run.
MAX_ROWS = 1000
# Warmup requests use a fixed, PII-free question, so user data is sent once.
WARMUP_QUESTION = "How long are KYC records kept?"
DEFAULT_API = os.environ.get("GATEWAY_URL", "http://localhost:8000")
DEFAULT_REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", Path(__file__).resolve().parents[1] / "reports"))


# Held in memory for the on-screen trace, never written to disk: masking can
# miss PII, so even masked questions and answers stay out of saved runs.
SESSION_ONLY_FIELDS = ("masked_question", "answer", "citation_detail")


class BatchError(ValueError):
    """The upload can't be used. Messages name the row, never its content (it may be PII)."""


@dataclass
class RequestResult:
    """One request's outcome, after masking. Holds no raw question text."""

    index: int
    status: int | None
    masked_question: str | None = None
    masked_entities: dict[str, int] = field(default_factory=dict)
    refused: bool | None = None
    citations: int | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)
    error: str | None = None  # error type or HTTP detail, never the request body
    # Session-only trace detail (never saved): the answer can echo PII that masking missed.
    answer: str | None = None
    citation_detail: list[dict] = field(default_factory=list)


def parse_batch(filename: str, data: bytes) -> list[str]:
    """Return the questions in a CSV (needs a `question` column) or JSONL (`{"question": ...}` per line) file."""
    try:
        text = data.decode("utf-8-sig")  # -sig: tolerate the BOM Excel writes
    except UnicodeDecodeError as exc:
        raise BatchError("File is not UTF-8 text.") from exc

    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        questions = _parse_csv(text)
    elif suffix in (".jsonl", ".ndjson"):
        questions = _parse_jsonl(text)
    else:
        raise BatchError("Upload a .csv or .jsonl file.")

    if not questions:
        raise BatchError("The file has no questions.")
    if len(questions) > MAX_ROWS:
        raise BatchError(f"The file has {len(questions)} questions; the limit is {MAX_ROWS}.")
    for i, q in enumerate(questions, start=1):
        if not q.strip():
            raise BatchError(f"Question {i} is empty.")
        if len(q) > MAX_QUESTION_CHARS:
            raise BatchError(f"Question {i} is longer than {MAX_QUESTION_CHARS} characters.")
    return questions


def _parse_csv(text: str) -> list[str]:
    reader = csv.DictReader(io.StringIO(text))
    columns = {name.strip().lower(): name for name in (reader.fieldnames or [])}
    if "question" not in columns:
        raise BatchError("The CSV needs a header row with a `question` column.")
    return [(row.get(columns["question"]) or "") for row in reader]


def _parse_jsonl(text: str) -> list[str]:
    questions = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BatchError(f"Line {line_no} is not valid JSON.") from exc
        if not isinstance(obj, dict) or not isinstance(obj.get("question"), str):
            raise BatchError(f'Line {line_no} needs a string "question" field.')
        questions.append(obj["question"])
    return questions


def file_sha256(data: bytes) -> str:
    """Fingerprint of the uploaded file, so a run can be tied to its exact input without storing it."""
    return hashlib.sha256(data).hexdigest()


def new_run_id(now: datetime | None = None) -> str:
    return f"dashboard-{(now or datetime.now(timezone.utc)).strftime('%Y%m%d-%H%M%S')}"


def send_one(http: httpx.Client, index: int, question: str, run_id: str) -> RequestResult:
    """POST one question; keep only the post-masking fields of the response."""
    try:
        r = http.post("/query", json={"question": question, "run_id": run_id})
    except httpx.HTTPError as exc:
        return RequestResult(index=index, status=None, error=type(exc).__name__)
    if r.status_code != 200:
        # The gateway's error bodies never echo input (see its 422 handler).
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
        return RequestResult(index=index, status=r.status_code, error=str(detail)[:200] if detail else None)
    body = r.json()
    return RequestResult(
        index=index,
        status=200,
        masked_question=body["masked_question"],
        masked_entities=body["masked_entities"],
        refused=body["refused"],
        citations=len(body["citations"]),
        timings_ms=body["timings_ms"],
        answer=body.get("answer"),
        citation_detail=body.get("citations", []),
    )


def run_batch(
    questions: list[str],
    api: str,
    run_id: str,
    warmup: int = 2,
    timeout_s: float = 600.0,
    on_result: Callable[[RequestResult], None] | None = None,
) -> list[RequestResult]:
    """Send warmups (run_id "<run_id>-warmup", excluded from metrics), then every question in order.

    Sequential on purpose: the local model server answers one request at a
    time, so concurrency would measure queueing, not the pipeline.
    """
    results = []
    with httpx.Client(base_url=api, timeout=timeout_s) as http:
        for _ in range(warmup):
            send_one(http, -1, WARMUP_QUESTION, f"{run_id}-warmup")
        for i, question in enumerate(questions):
            result = send_one(http, i, question, run_id)
            results.append(result)
            if on_result:
                on_result(result)
    return results


def get_json(api: str, path: str, timeout_s: float = 30.0) -> dict | None:
    """GET a gateway endpoint; None if it isn't reachable or returns an error."""
    try:
        r = httpx.get(f"{api}{path}", timeout=timeout_s)
    except httpx.HTTPError:
        return None
    return r.json() if r.status_code == 200 else None


def build_run_record(
    *,
    run_id: str,
    started_at: datetime,
    file_name: str,
    file_hash: str,
    warmup: int,
    results: list[RequestResult],
    gateway_before: dict | None,
    gateway_after: dict | None,
    metrics: dict | None,
    wall_time_s: float,
) -> dict:
    """Everything needed to report on and reproduce a batch run, minus the raw questions."""
    entity_totals: Counter[str] = Counter()
    for r in results:
        entity_totals.update(r.masked_entities)
    return {
        "kind": "dashboard_batch",
        "run_id": run_id,
        "label": "Dashboard batch run (uploaded questions; only masked text is stored)",
        "started_at": started_at.isoformat(),
        "input": {"file_name": file_name, "sha256": file_hash, "questions": len(results), "warmup": warmup},
        "gateway": gateway_before,
        "environment": {
            # Where the gateway runs (Docker Desktop's Linux VM on macOS), not the Mac.
            "gateway_load_avg_before": (gateway_before or {}).get("load_avg"),
            "gateway_load_avg_after": (gateway_after or {}).get("load_avg"),
            "gateway_cpus": (gateway_before or {}).get("cpus"),
            "wall_time_s": round(wall_time_s, 1),
        },
        "status_counts": dict(Counter(str(r.status) for r in results)),
        "masked_entity_totals": dict(entity_totals),
        "refused": sum(bool(r.refused) for r in results),
        "latency": metrics,
        # No question text at all, not even masked: the canary audit shows the
        # Trust Engine misses some PII, so "masked" text can still hold it.
        "requests": [{k: v for k, v in asdict(r).items() if k not in SESSION_ONLY_FIELDS} for r in results],
    }


def save_run(record: dict, reports_dir: Path = DEFAULT_REPORTS_DIR) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"dashboard_batch_{record['run_id'].removeprefix('dashboard-')}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def execute(
    file_name: str,
    data: bytes,
    api: str = DEFAULT_API,
    warmup: int = 2,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    on_result: Callable[[RequestResult], None] | None = None,
) -> tuple[dict, Path]:
    """Parse, run, fetch the gateway's percentiles, and save. Used by the dashboard and the CLI."""
    questions = parse_batch(file_name, data)
    gateway_before = get_json(api, "/info")
    if gateway_before is None:
        raise ConnectionError(f"Gateway not reachable at {api}")
    started_at = datetime.now(timezone.utc)
    run_id = new_run_id(started_at)
    t0 = time.perf_counter()
    results = run_batch(questions, api, run_id, warmup=warmup, on_result=on_result)
    wall_time_s = time.perf_counter() - t0
    record = build_run_record(
        run_id=run_id,
        started_at=started_at,
        file_name=Path(file_name).name,
        file_hash=file_sha256(data),
        warmup=warmup,
        results=results,
        gateway_before=gateway_before,
        gateway_after=get_json(api, "/info"),
        # The gateway computes percentiles from the stored samples (same code as every script).
        metrics=get_json(api, f"/metrics/{run_id}"),
        wall_time_s=wall_time_s,
    )
    return record, save_run(record, reports_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a CSV/JSONL question batch through the gateway.")
    parser.add_argument("file", type=Path)
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--warmup", type=int, default=2)
    args = parser.parse_args()

    def progress(r: RequestResult) -> None:
        # Status and timing only: never the question.
        print(f"[{r.index + 1}] status={r.status} total_ms={r.timings_ms.get('total', 0):.0f}", flush=True)

    try:
        record, path = execute(args.file.name, args.file.read_bytes(), args.api, args.warmup, on_result=progress)
    except (BatchError, ConnectionError) as exc:
        sys.exit(str(exc))
    stages = (record["latency"] or {}).get("stages", {})
    for stage in STAGES:
        if stage in stages:
            s = stages[stage]
            print(f"{stage:<20} n={s['n']:<4} p50={s['p50']:.0f} p95={s['p95']:.0f} p99={s['p99']:.0f} ms")
    print(f"Saved {path}")


if __name__ == "__main__":
    main()
