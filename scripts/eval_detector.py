"""Measure Trust Engine PII detection: precision and recall per entity type.

Runs the production `detect()` over the labeled synthetic dataset from
scripts/generate_dataset.py and compares predicted spans to gold spans.

Matching rule (per entity type):
  - true positive:  a gold span overlapped by a prediction of the SAME type
  - false negative: a gold span with no same-type overlapping prediction
  - false positive: a prediction overlapping no gold span of its type
A span found as the wrong type therefore counts as a miss for the true type
AND a false positive for the predicted one. Exact-boundary matches are
reported separately, since a partial mask can still leak characters.

Usage (host, no db/Ollama needed):
    .venv/bin/python scripts/generate_dataset.py
    .venv/bin/python scripts/eval_detector.py

Writes reports/eval_detector_<timestamp>.json. The examples in it are
synthetic values from the generated dataset.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.trust_engine.engine import PII_ENTITIES, SCORE_THRESHOLD, detect, get_analyzer, scrubber_id  # noqa: E402

DEFAULT_DATASET = REPO_ROOT / "data" / "generated" / "pii_dataset.jsonl"
REPORTS_DIR = REPO_ROOT / "reports"
EXAMPLES_PER_BUCKET = 5


def overlaps(a: dict, b: dict) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


def score_record(record: dict, predictions: list[dict], counts: dict, examples: dict) -> None:
    """Update per-type TP/FP/FN counts (and a few examples) for one record."""
    gold = record["spans"]
    for g in gold:
        same_type = [p for p in predictions if p["entity_type"] == g["entity_type"] and overlaps(p, g)]
        c = counts[g["entity_type"]]
        if same_type:
            c["tp"] += 1
            if any(p["start"] == g["start"] and p["end"] == g["end"] for p in same_type):
                c["exact"] += 1
        else:
            c["fn"] += 1
            found_as = [p["entity_type"] for p in predictions if overlaps(p, g)]
            _add_example(examples, g["entity_type"], "missed", record, g, found_as)

    for p in predictions:
        if not any(g["entity_type"] == p["entity_type"] and overlaps(p, g) for g in gold):
            counts[p["entity_type"]]["fp"] += 1
            _add_example(examples, p["entity_type"], "false_positive", record, p, [])


def _add_example(examples: dict, entity_type: str, kind: str, record: dict, span: dict, found_as: list[str]) -> None:
    bucket = examples[entity_type][kind]
    if len(bucket) < EXAMPLES_PER_BUCKET:
        item = {"template": record["template"], "text": record["text"], "span": record["text"][span["start"]:span["end"]]}
        if found_as:
            item["detected_as"] = found_as
        bucket.append(item)


def metrics(c: dict) -> dict:
    """Precision, recall, F1 from counts. None where undefined (no predictions or no gold)."""
    predicted, gold = c["tp"] + c["fp"], c["tp"] + c["fn"]
    precision = c["tp"] / predicted if predicted else None
    recall = c["tp"] / gold if gold else None
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else None
    return {**c, "support": gold, "precision": precision, "recall": recall, "f1": f1}


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def markdown_table(per_type: dict, overall: dict) -> str:
    """The accuracy table, formatted for docs/build-log.md."""
    lines = [
        "| Entity | Support | Precision | Recall | F1 | TP | FP | FN | Exact span |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, m in list(per_type.items()) + [("**Overall (micro)**", overall)]:
        exact = f"{m['exact']}/{m['tp']}" if m["tp"] else "—"
        lines.append(
            f"| {name} | {m['support']} | {_pct(m['precision'])} | {_pct(m['recall'])} | {_pct(m['f1'])} "
            f"| {m['tp']} | {m['fp']} | {m['fn']} | {exact} |"
        )
    return "\n".join(lines)


def _git_commit() -> str | None:
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
                                capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True).stdout
        return commit + ("-dirty" if dirty.strip() else "")
    except (OSError, subprocess.CalledProcessError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate PII detection precision and recall per entity type.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()
    if not args.dataset.exists():
        sys.exit(f"{args.dataset} not found. Run: .venv/bin/python scripts/generate_dataset.py")

    raw = args.dataset.read_bytes()
    records = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]

    t0 = time.perf_counter()
    get_analyzer()  # model load, timed separately from per-record scans
    load_ms = (time.perf_counter() - t0) * 1000

    counts: dict = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0, "exact": 0})
    examples: dict = defaultdict(lambda: {"missed": [], "false_positive": []})
    by_difficulty: dict = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0, "exact": 0})
    by_template: dict = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0, "exact": 0})
    negatives_flagged = 0
    scan_ms: list[float] = []

    for record in records:
        t = time.perf_counter()
        detections = detect(record["text"])
        scan_ms.append((time.perf_counter() - t) * 1000)
        predictions = [{"entity_type": d.entity_type, "start": d.start, "end": d.end} for d in detections]

        score_record(record, predictions, counts, examples)
        # Same scoring, bucketed by difficulty and template (examples not needed here).
        diff_counts: dict = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0, "exact": 0})
        score_record(record, predictions, diff_counts, defaultdict(lambda: {"missed": [], "false_positive": []}))
        for c in diff_counts.values():
            for k in c:
                by_difficulty[record["difficulty"]][k] += c[k]
                by_template[(record["difficulty"], record["template"])][k] += c[k]
        if record["difficulty"] == "negative" and predictions:
            negatives_flagged += 1

    per_type = {e: metrics(counts[e]) for e in PII_ENTITIES}
    totals = {k: sum(counts[e][k] for e in PII_ENTITIES) for k in ("tp", "fp", "fn", "exact")}
    overall = metrics(totals)
    negatives = sum(r["difficulty"] == "negative" for r in records)
    latency = {
        "model_load_ms": round(load_ms, 1),
        "per_record_mean_ms": round(float(np.mean(scan_ms)), 2),
        "per_record_p50_ms": round(float(np.percentile(scan_ms, 50)), 2),
        "per_record_p95_ms": round(float(np.percentile(scan_ms, 95)), 2),
    }

    table = markdown_table(per_type, overall)
    print(table)
    print("\nBy difficulty (micro):")
    for name in ("standard", "hard"):
        m = metrics(by_difficulty[name])
        print(f"  {name:<9} precision {_pct(m['precision']):>6}  recall {_pct(m['recall']):>6}  (support {m['support']}, FP {m['fp']})")
    print(f"  negative  {negatives_flagged}/{negatives} no-PII records had a detection ({by_difficulty['negative']['fp']} false positives)")

    print("\nBy template (recall, then false positives):")
    for (difficulty, template), c in sorted(by_template.items()):
        m = metrics(c)
        print(f"  {difficulty:<9} {template:<20} recall {_pct(m['recall']):>6}  FN {c['fn']:>3}  FP {c['fp']:>2}")
    print(f"pii_scan per record: mean {latency['per_record_mean_ms']} ms, P50 {latency['per_record_p50_ms']} ms, "
          f"P95 {latency['per_record_p95_ms']} ms (model load {latency['model_load_ms']:.0f} ms, excluded)")

    REPORTS_DIR.mkdir(exist_ok=True)
    started = datetime.now(timezone.utc)
    out_path = REPORTS_DIR / f"eval_detector_{started.strftime('%Y%m%d-%H%M%S')}.json"
    report = {
        "started_at": started.isoformat(),
        "git_commit": _git_commit(),
        "scrubber": scrubber_id(),
        "score_threshold": SCORE_THRESHOLD,
        "dataset": {
            "path": str(args.dataset.relative_to(REPO_ROOT)) if args.dataset.is_relative_to(REPO_ROOT) else str(args.dataset),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "records": len(records),
            "synthetic": True,
        },
        "matching": "same-type overlap; exact-boundary counts reported separately",
        "per_entity": per_type,
        "overall_micro": overall,
        "by_difficulty": {k: metrics(v) for k, v in by_difficulty.items()},
        "by_template": {f"{d}/{t}": metrics(v) for (d, t), v in sorted(by_template.items())},
        "negative_records_flagged": {"flagged": negatives_flagged, "total": negatives},
        "latency": latency,
        "examples_synthetic": {e: examples[e] for e in PII_ENTITIES if e in examples},
        "markdown_table": table,
    }
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nSaved {out_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
