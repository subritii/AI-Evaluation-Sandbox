"""Store per-stage latency samples and summarize them as avg and percentiles.

Why percentiles: averages hide the slow requests users actually notice. P95
means "95% of requests were at least this fast". But a percentile is only as
good as the samples behind it: with n samples, P99 is decided by roughly the
slowest n/100 requests. At n=200 that's about 2 requests, so one hiccup moves
it a lot. The summary reports n with every stage and flags small samples.
"""

import uuid

import numpy as np
import psycopg

STAGES = ("pii_scan", "retrieval", "time_to_first_token", "generation", "total")
PERCENTILES = (50, 90, 95, 99)

# Below this many samples, P99 rests on a single observation (1% of 100 = 1).
MIN_SAMPLES_FOR_P99 = 100


def record_samples(conn: psycopg.Connection, run_id: str, request_id: uuid.UUID, timings_ms: dict[str, float]) -> None:
    """Insert one row per measured stage. Stages that didn't run (e.g. no generation) are skipped."""
    rows = [(run_id, request_id, stage, timings_ms[stage]) for stage in STAGES if stage in timings_ms]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany("INSERT INTO latency_samples (run_id, request_id, stage, ms) VALUES (%s, %s, %s, %s)", rows)


def summarize(values: list[float]) -> dict:
    """avg, P50/P90/P95/P99, min, max (ms) for one stage. Uses numpy's default linear interpolation."""
    arr = np.asarray(values, dtype=float)
    summary = {"n": int(arr.size), "avg": round(float(arr.mean()), 2)}
    for p in PERCENTILES:
        summary[f"p{p}"] = round(float(np.percentile(arr, p)), 2)
    summary["min"] = round(float(arr.min()), 2)
    summary["max"] = round(float(arr.max()), 2)
    return summary


def run_metrics(conn: psycopg.Connection, run_id: str) -> dict | None:
    """Summarize every stage recorded for `run_id`; None if the run has no samples."""
    rows = conn.execute(
        "SELECT stage, ms FROM latency_samples WHERE run_id = %s", (run_id,)
    ).fetchall()
    if not rows:
        return None
    by_stage: dict[str, list[float]] = {}
    for stage, ms in rows:
        by_stage.setdefault(stage, []).append(ms)

    stages = {stage: summarize(by_stage[stage]) for stage in STAGES if stage in by_stage}
    requests = conn.execute(
        "SELECT count(DISTINCT request_id) FROM latency_samples WHERE run_id = %s", (run_id,)
    ).fetchone()[0]
    notes = [
        f"{stage}: only {s['n']} samples; P99 is not reliable below {MIN_SAMPLES_FOR_P99}."
        for stage, s in stages.items()
        if s["n"] < MIN_SAMPLES_FOR_P99
    ]
    return {"run_id": run_id, "requests": requests, "unit": "ms", "stages": stages, "notes": notes}
