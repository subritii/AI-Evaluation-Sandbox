"""Canary leakage audit: plant fake PII, run a batch through the API, search everywhere.

A canary is a unique, synthetic PII value (name, SSN, card, IBAN, routing and
account numbers, email, phone) planted in a realistic question. After the
batch, every canary is searched for in:

  response.masked_question   what the embedding model and LLM actually received
  response.answer / other    what the API returned
  logs                       backend, ollama, and db container logs for the run; with
                             native Ollama, its server log if given (--ollama-log),
                             otherwise reported as NOT searched
  database                   every text/json column in every public table

A canary found anywhere is a leak. You can't prove a negative, but you can
check for known planted values everywhere data could land. The batch also
produces the latency breakdown (GET /metrics/{run_id}).

All canaries are SYNTHETIC (Faker, fixed seed) and the run is labeled as a
canary run (run_id "canary-..."). Warmup requests use "<run_id>-warmup" and are
excluded from the metrics.

Usage (gateway up: docker compose up -d):
    .venv/bin/python scripts/canary_audit.py                  # 200 requests
    .venv/bin/python scripts/canary_audit.py --requests 20    # quick check

Writes reports/canary_audit_<timestamp>.json.
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from app.config import get_settings  # noqa: E402
from generate_dataset import Values  # noqa: E402

REPORTS_DIR = REPO_ROOT / "reports"
LOG_SERVICES = ("backend", "ollama", "db")
STAGES = ("pii_scan", "retrieval", "time_to_first_token", "generation", "total")

# Clean policy questions (no PII) that make up the rest of the batch.
CLEAN_QUESTIONS = [
    "Which wire transfers need a callback?",
    "When must a CVV be deleted?",
    "Within how many hours must we notify our federal regulator of an incident?",
    "How long are KYC records kept?",
    "When must a Currency Transaction Report be filed?",
    "How often must managers recertify access to Restricted systems?",
    "How long are AI assistant prompt and response logs retained?",
    "What is the bank's dress code?",
]


def canary_templates(v: Values) -> dict:
    """Question templates with planted canaries: {name: (text parts, ...)}. Labeled slots are (type, value)."""
    return {
        "kyc_customer": lambda: ["I'm ", ("PERSON", v.person()), " (SSN ", ("US_SSN", v.ssn()), "). How long are KYC records kept?"],
        "card_charge": lambda: ["My card ", ("CREDIT_CARD", v.card()), " was charged twice. When must a CVV be deleted?"],
        "wire_details": lambda: ["Wire to routing number ", ("US_ROUTING_NUMBER", v.routing()), ", account ",
                                 ("ACCOUNT_NUMBER", v.account()), ". Which wire transfers need a callback?"],
        "contact_breach": lambda: ["Customer ", ("PERSON", v.person()), ", email ", ("EMAIL_ADDRESS", v.email()), ", phone ",
                                   ("PHONE_NUMBER", v.phone()), ", reported a lost statement. How quickly must a data breach be reported?"],
        "iban_wire": lambda: ["Beneficiary IBAN ", ("IBAN_CODE", v.iban()), ". What approval is needed for a $300,000 wire transfer?"],
        # Realistic but hard: no context words say what the numbers are.
        "no_context_wire": lambda: ["Send the funds to ", ("US_ROUTING_NUMBER", v.routing()), " / ",
                                    ("ACCOUNT_NUMBER", v.account()), " for ", ("PERSON", v.person()),
                                    ". Within how many hours must we notify our federal regulator of an incident?"],
    }


def build_question(parts: list) -> tuple[str, list[dict]]:
    """Join template parts; return the question and its planted canaries."""
    text, canaries = "", []
    for part in parts:
        if isinstance(part, tuple):
            entity_type, value = part
            canaries.append({"entity_type": entity_type, "value": value})
            text += value
        else:
            text += part
    return text, canaries


def make_batch(n: int, canary_share: float, seed: int) -> list[dict]:
    """A reproducible batch: `canary_share` of requests carry canaries, the rest are clean questions."""
    rng = random.Random(seed)
    values = Values(seed)
    templates = canary_templates(values)
    batch = []
    for i in range(n):
        if rng.random() < canary_share:
            name = rng.choice(sorted(templates))
            question, canaries = build_question(templates[name]())
        else:
            name, question, canaries = "clean", rng.choice(CLEAN_QUESTIONS), []
        batch.append({"index": i, "template": name, "question": question, "canaries": canaries})
    return batch


# --- Searching ---

_SEPARATORS = re.compile(r"[\s\-.()]+")
# Compact canaries shorter than this are only matched exactly (avoids chance hits).
MIN_COMPACT_LENGTH = 8


def compact(text: str) -> str:
    """Lowercase and drop spaces, dashes, dots, and parentheses, so a reformatted value still matches:
    'GB82 WEST 1234' -> 'gb82west1234', '(212) 555-0187' -> '2125550187'."""
    return _SEPARATORS.sub("", text.lower())


def canary_found_in(value: str, haystack: str, haystack_compact: str) -> bool:
    """Exact (case-insensitive) match, or a separator-insensitive match for long enough values."""
    if value.lower() in haystack.lower():
        return True
    value_compact = compact(value)
    return len(value_compact) >= MIN_COMPACT_LENGTH and value_compact in haystack_compact


def collect_logs(since: str, native_ollama_log: Path | None, native_log_offset: int) -> dict[str, str]:
    """Logs for the run window, per service.

    Container logs come from `docker compose logs`. With native Ollama the
    `ollama` container isn't running, so its (empty) container log would look
    like a clean result; the native server log is read instead, from the byte
    offset taken when the batch started.
    """
    logs = {}
    for service in LOG_SERVICES:
        out = subprocess.run(
            ["docker", "compose", "logs", "--no-color", "--since", since, service],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        logs[service] = out.stdout + out.stderr
    if native_ollama_log is not None:
        with native_ollama_log.open("rb") as f:
            f.seek(native_log_offset)
            logs["ollama_native"] = f.read().decode("utf-8", errors="replace")
    return logs


def collect_database_text() -> dict[str, str]:
    """Every text-like column in every public table, as one string per table.column."""
    settings = get_settings()
    columns: dict[str, str] = {}
    with psycopg.connect(settings.database_url) as conn:
        rows = conn.execute(
            """
            SELECT table_name, column_name FROM information_schema.columns
            WHERE table_schema = 'public'
              AND data_type IN ('text', 'character varying', 'character', 'json', 'jsonb', 'uuid')
            ORDER BY table_name, column_name
            """
        ).fetchall()
        for table, column in rows:
            values = conn.execute(
                f'SELECT "{column}"::text FROM "{table}" WHERE "{column}" IS NOT NULL'  # names from the catalog
            ).fetchall()
            columns[f"{table}.{column}"] = "\n".join(v[0] for v in values)
    return columns


def scan(batch: list[dict], responses: list[dict], logs: dict[str, str], db: dict[str, str]) -> list[dict]:
    """Return one finding per (canary, location) where a planted value appears."""
    locations: list[tuple[str, int | None, str]] = []  # (location, request index or None, text)
    for item, resp in zip(batch, responses):
        body = resp.get("body") or {}
        locations.append(("response.masked_question", item["index"], body.get("masked_question", "")))
        locations.append(("response.answer", item["index"], body.get("answer", "")))
        other = {k: v for k, v in body.items() if k not in ("masked_question", "answer")} if body else resp.get("text", "")
        locations.append(("response.other_fields", item["index"], json.dumps(other)))
    locations += [(f"logs.{svc}", None, text) for svc, text in logs.items()]
    locations += [(f"database.{col}", None, text) for col, text in db.items()]

    indexed = [(loc, idx, text, compact(text)) for loc, idx, text in locations]
    findings = []
    for item in batch:
        for canary in item["canaries"]:
            for loc, idx, text, text_compact in indexed:
                # A response only "leaks" its own request's canaries; logs and DB are shared.
                if idx is not None and idx != item["index"]:
                    continue
                if canary_found_in(canary["value"], text, text_compact):
                    findings.append({"request": item["index"], "template": item["template"],
                                     "entity_type": canary["entity_type"], "location": loc,
                                     "value_synthetic": canary["value"]})
    return findings


# --- Running ---

def run_batch(api: str, batch: list[dict], run_id: str, timeout_s: float) -> list[dict]:
    """Send every request sequentially (the CPU model server handles one at a time anyway)."""
    responses = []
    with httpx.Client(base_url=api, timeout=timeout_s) as http:
        for item in batch:
            t = time.perf_counter()
            try:
                r = http.post("/query", json={"question": item["question"], "run_id": run_id})
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
                responses.append({"status": r.status_code, "body": body if r.status_code == 200 else None,
                                  "text": r.text if r.status_code != 200 else None})
            except httpx.HTTPError as exc:
                responses.append({"status": None, "body": None, "text": f"{type(exc).__name__}"})
            done = len(responses)
            print(f"[{done}/{len(batch)}] {item['template']:<16} status={responses[-1]['status']} "
                  f"{(time.perf_counter() - t):.1f}s", flush=True)
    return responses


def print_latency(metrics: dict) -> None:
    print(f"\nLatency breakdown, run {metrics['run_id']} ({metrics['requests']} requests, ms):")
    print(f"  {'stage':<20} {'n':>4} {'avg':>9} {'p50':>9} {'p90':>9} {'p95':>9} {'p99':>9}")
    for stage in STAGES:
        s = metrics["stages"].get(stage)
        if s:
            print(f"  {stage:<20} {s['n']:>4} {s['avg']:>9.0f} {s['p50']:>9.0f} {s['p90']:>9.0f} {s['p95']:>9.0f} {s['p99']:>9.0f}")
    for note in metrics.get("notes", []):
        print(f"  note: {note}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Plant canary PII, run a batch through the API, and scan for leaks.")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=3, help="Clean warmup requests, excluded from metrics")
    parser.add_argument("--canary-share", type=float, default=0.5, help="Share of requests that carry canaries")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--api", default=os.environ.get("GATEWAY_URL", "http://localhost:8000"))
    parser.add_argument("--timeout", type=float, default=600.0, help="Per-request timeout (s)")
    parser.add_argument("--ollama-log", type=Path, default=None,
                        help="Native Ollama server log to search (e.g. from `ollama serve > file 2>&1`)")
    args = parser.parse_args()

    started = datetime.now(timezone.utc)
    run_id = f"canary-{started.strftime('%Y%m%d-%H%M%S')}"
    batch = make_batch(args.requests, args.canary_share, args.seed)
    load_before = os.getloadavg()

    with httpx.Client(base_url=args.api, timeout=10) as http:
        http.get("/health").raise_for_status()

    print(f"Warmup: {args.warmup} clean requests (run_id {run_id}-warmup, excluded)")
    warmup = [{"index": -1, "template": "warmup", "question": q, "canaries": []} for q in CLEAN_QUESTIONS[: args.warmup]]
    run_batch(args.api, warmup, f"{run_id}-warmup", args.timeout)

    since = datetime.now(timezone.utc).isoformat()
    native_log_offset = args.ollama_log.stat().st_size if args.ollama_log else 0
    print(f"Batch: {len(batch)} requests, run_id {run_id}")
    t0 = time.perf_counter()
    responses = run_batch(args.api, batch, run_id, args.timeout)
    wall_s = time.perf_counter() - t0
    load_after = os.getloadavg()

    with httpx.Client(base_url=args.api, timeout=30) as http:
        metrics_resp = http.get(f"/metrics/{run_id}")
    metrics = metrics_resp.json() if metrics_resp.status_code == 200 else None
    # The gateway records its own backend and models with each sample; trust that
    # over this script's environment, which may differ from the gateway's.
    models = (metrics or {}).get("models", [])
    backends = {m["model_backend"] for m in models}

    db_text = collect_database_text()
    logs = collect_logs(since, args.ollama_log, native_log_offset)
    not_searched = []
    if "native" in backends:
        logs.pop("ollama")  # the stopped container's log says nothing about native Ollama
        if args.ollama_log is None:
            not_searched.append("native Ollama server log (no --ollama-log given)")
    findings = scan(batch, responses, logs, db_text)

    planted = [c for item in batch for c in item["canaries"]]
    planted_by_type = Counter(c["entity_type"] for c in planted)
    leaked_keys = {(f["request"], f["value_synthetic"]) for f in findings}
    leaked_by_type = Counter(f["entity_type"] for f in {(f["request"], f["value_synthetic"]): f for f in findings}.values())
    by_location = defaultdict(Counter)
    for f in findings:
        by_location[f["location"]][f["entity_type"]] += 1
    status_counts = Counter(str(r["status"]) for r in responses)

    if metrics:
        print_latency(metrics)
    for m in models:
        print(f"Model config (from gateway): backend={m['model_backend']} llm={m['llm_model']} "
              f"embed={m['embed_model']} ({m['requests']} requests)")
    print(f"\nRequests: {dict(status_counts)}   wall time {wall_s / 60:.1f} min   "
          f"host load avg (1m) {load_before[0]:.1f} -> {load_after[0]:.1f}")
    print(f"\nCanaries planted: {len(planted)} in {sum(bool(i['canaries']) for i in batch)} requests (synthetic)")
    print(f"Canaries found (leaked anywhere): {len(leaked_keys)}/{len(planted)}")
    print(f"  {'entity':<20} {'planted':>8} {'found':>6}")
    for entity, n in sorted(planted_by_type.items()):
        print(f"  {entity:<20} {n:>8} {leaked_by_type.get(entity, 0):>6}")
    print("  by location:")
    all_locations = ["response.masked_question", "response.answer", "response.other_fields"] + \
        [f"logs.{s}" for s in logs] + ["database (all text columns)"]
    db_hits = sum(sum(c.values()) for loc, c in by_location.items() if loc.startswith("database."))
    for loc in all_locations:
        hits = db_hits if loc.startswith("database") else sum(by_location.get(loc, Counter()).values())
        print(f"    {loc:<28} {hits}")
    for item in not_searched:
        print(f"    NOT searched: {item}")

    REPORTS_DIR.mkdir(exist_ok=True)
    out_path = REPORTS_DIR / f"canary_audit_{started.strftime('%Y%m%d-%H%M%S')}.json"
    report = {
        "run_id": run_id,
        "label": "SYNTHETIC canary audit: all planted values are fake (Faker, fixed seed)",
        "started_at": started.isoformat(),
        "git_commit": _git_commit(),
        "config": {"requests": args.requests, "warmup": args.warmup, "canary_share": args.canary_share,
                   "seed": args.seed, "api": args.api},
        "environment": {"host_load_avg_before": load_before, "host_load_avg_after": load_after,
                        "wall_time_s": round(wall_s, 1)},
        "model_config": models,
        "status_counts": dict(status_counts),
        "latency": metrics,
        "planted": {"total": len(planted), "by_entity": dict(planted_by_type)},
        "found": {"total": len(leaked_keys), "by_entity": dict(leaked_by_type),
                  "by_location": {k: dict(v) for k, v in sorted(by_location.items())}},
        "searched_locations": {"responses": ["masked_question", "answer", "other_fields"],
                               "logs": sorted(logs), "database_columns": sorted(db_text)},
        "not_searched": not_searched,
        "findings": findings,
        "requests": [{**item, "status": r["status"],
                      "masked_entities": (r["body"] or {}).get("masked_entities")} for item, r in zip(batch, responses)],
    }
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nSaved {out_path.relative_to(REPO_ROOT)}")


def _git_commit() -> str | None:
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
                                capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True).stdout
        return commit + ("-dirty" if dirty.strip() else "")
    except (OSError, subprocess.CalledProcessError):
        return None


if __name__ == "__main__":
    main()
