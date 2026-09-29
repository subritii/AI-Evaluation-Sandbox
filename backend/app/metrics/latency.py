"""Store per-stage latency samples and summarize them as avg and percentiles.

Why percentiles: averages hide the slow requests users actually notice. P95
means "95% of requests were at least this fast". But a percentile is only as
good as the samples behind it: with n samples, P99 is decided by roughly the
slowest n/100 requests. At n=200 that's about 2 requests, so one hiccup moves
it a lot. The summary reports n with every stage and flags small samples.
"""

import uuid
from dataclasses import dataclass

import numpy as np
import psycopg

STAGES = ("pii_scan", "retrieval", "time_to_first_token", "generation", "total")
PERCENTILES = (50, 90, 95, 99)

# Below this many samples, P99 rests on a single observation (1% of 100 = 1).
MIN_SAMPLES_FOR_P99 = 100


@dataclass(frozen=True)
class ModelConfig:
    """The model runtime and models behind a sample; timings mean little without it."""

    model_backend: str  # "docker", "native", or "unknown"
    llm_model: str
    embed_model: str


def record_samples(
    conn: psycopg.Connection, run_id: str, request_id: uuid.UUID, timings_ms: dict[str, float], model: ModelConfig
) -> None:
    """Insert one row per measured stage. Stages that didn't run (e.g. no generation) are skipped."""
    rows = [
        (run_id, request_id, stage, timings_ms[stage], model.model_backend, model.llm_model, model.embed_model)
        for stage in STAGES
        if stage in timings_ms
    ]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO latency_samples (run_id, request_id, stage, ms, model_backend, llm_model, embed_model)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            rows,
        )


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
    # Normally one configuration per run; a run that spans a switch lists each.
    models = [
        {"model_backend": b, "llm_model": llm, "embed_model": emb, "requests": n}
        for b, llm, emb, n in conn.execute(
            """
            SELECT model_backend, llm_model, embed_model, count(DISTINCT request_id)
            FROM latency_samples WHERE run_id = %s GROUP BY 1, 2, 3 ORDER BY 4 DESC
            """,
            (run_id,),
        ).fetchall()
    ]
    notes = [
        f"{stage}: only {s['n']} samples; P99 is not reliable below {MIN_SAMPLES_FOR_P99}."
        for stage, s in stages.items()
        if s["n"] < MIN_SAMPLES_FOR_P99
    ]
    if len(models) > 1:
        notes.append("Run mixes model configurations; percentiles combine them.")
    return {"run_id": run_id, "requests": requests, "models": models, "unit": "ms", "stages": stages, "notes": notes}
