"""HTML report: sections, provenance, honesty about missing data, and self-containment."""

import re
from datetime import datetime, timezone
from pathlib import Path

from report_html import build_report, combine_notes
from reports import summarize_airgap, summarize_canary, summarize_detector, summarize_eval_rag

STAGES = {s: {"n": 200, "avg": 10.0, "p50": 9.0, "p90": 12.0, "p95": 15.0, "p99": 20.0, "min": 1.0, "max": 30.0}
          for s in ("pii_scan", "retrieval", "time_to_first_token", "generation", "total")}

BATCH = {
    "run_id": "dashboard-20260101-000000", "started_at": "2026-01-01T00:00:00+00:00",
    "input": {"file_name": "b<script>.csv", "sha256": "ab" * 32, "questions": 200, "warmup": 2},
    "gateway": {"model_backend": "native", "llm_model": "llama3.2:3b", "embed_model": "nomic-embed-text",
                "temperature": 0.0, "max_tokens": 256, "scrubber": "presidio-x+rules-v2"},
    "environment": {"gateway_load_avg_before": [1.5, 1, 1], "gateway_load_avg_after": [2.5, 1, 1], "gateway_cpus": 8},
    "status_counts": {"200": 200}, "masked_entity_totals": {"PERSON": 3}, "refused": 4,
    "latency": {"stages": STAGES, "notes": []},
}

RAG = summarize_eval_rag(Path("eval_rag_1.json"), {
    "started_at": "t", "git_commit": "abc",
    "config": {"model_backend": "docker", "llm_model": "llama3.2:3b", "embed_model": "nomic-embed-text"},
    "environment": {"host_load_avg_before": [3.0, 1, 1], "host_load_avg_after": [4.0, 1, 1]},
    "results": [
        {"id": "q1", "should_refuse": False, "retrieval_hit": True, "expected_rank": 1, "answer_pass": True,
         "citation_pass": True, "citation_precision": 1.0, "timings_ms": {"total": 900}},
        {"id": "q2", "should_refuse": True, "retrieval_hit": None, "answer_pass": True, "citation_pass": True,
         "citation_precision": None, "timings_ms": {"total": 500}},
    ],
})

DET = summarize_detector(Path("eval_detector_1.json"), {
    "started_at": "t", "git_commit": "abc", "scrubber": "presidio-x+rules-v2", "score_threshold": 0.4,
    "dataset": {"records": 1000, "sha256": "cd" * 32},
    "per_entity": {"PERSON": {"support": 10, "precision": 1.0, "recall": 0.9, "tp": 9, "fp": 0, "fn": 1}},
    "overall_micro": {"support": 10, "precision": 1.0, "recall": 0.9, "tp": 9, "fp": 0, "fn": 1},
    "by_difficulty": {"hard": {"recall": 0.5, "precision": 1.0, "support": 4}},
    "negative_records_flagged": {"flagged": 3, "total": 150},
})


def finding(request, value, location, entity="PERSON"):
    return {"request": request, "value_synthetic": value, "location": location, "entity_type": entity}


def canary(not_searched, findings=None, latency_n=200):
    stages = {s: dict(v, n=latency_n) for s, v in STAGES.items()}
    report = {
        "run_id": "canary-1", "started_at": "t", "git_commit": "abc",
        "config": {"requests": 200}, "environment": {"host_load_avg_before": [2.2, 1, 1], "host_load_avg_after": [4.1, 1, 1]},
        "model_config": [{"model_backend": "native", "llm_model": "llama3.2:3b", "embed_model": "e", "requests": 200}],
        "planted": {"total": 207, "by_entity": {"PERSON": 51, "US_SSN": 21}},
        "found": {"total": 9, "by_entity": {"PERSON": 9}, "by_location": {"response.masked_question": {"PERSON": 9}}},
        "searched_locations": {"responses": ["masked_question"], "logs": ["backend"], "database_columns": ["a.b"]},
        "findings": findings if findings is not None else
        [finding(i, f"Name {i}", "response.masked_question") for i in range(9)],
        "latency": {"stages": stages, "notes": []},
    }
    if not_searched is not ...:
        report["not_searched"] = not_searched
    return summarize_canary(Path("canary_audit_1.json"), report)


def build(**overrides):
    args = {"batch": BATCH, "rag": RAG, "det": DET, "can": canary([]), "airgap": None} | overrides
    return build_report(generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc), **args)


def test_all_sections_and_key_numbers_present():
    html = build()
    for text in ("1. Batch run", "2. RAG answer quality", "3. PII detector accuracy", "4. Canary leakage audit",
                 "5. Methodology", "Known gaps", "90.0%", "3 of 150", "native Ollama"):
        assert text in html, text


def test_every_section_names_its_source():
    html = build()
    for source in ("eval_rag_1.json", "eval_detector_1.json", "canary_audit_1.json", "dashboard-20260101-000000"):
        assert source in html


def test_methodology_records_backend_models_and_load():
    html = build()
    assert "llama3.2:3b" in html and "presidio-x+rules-v2" in html
    assert "1.5 → 2.5" in html and "3.0 → 4.0" in html and "2.2 → 4.1" in html
    assert "outside that VM" in html  # native backend: VM load excludes the models


def test_backend_mismatch_is_flagged():
    # RAG ran on Docker, the batch on native: the report must say so.
    assert "The RAG eval ran on Docker Ollama" in build()


def test_missing_runs_say_so_instead_of_inventing_numbers():
    html = build(batch=None, rag=None, det=None, can=None)
    assert "No batch run selected" in html and "No RAG eval report found" in html
    assert "No detector eval report found" in html and "No canary audit report found" in html


def test_canary_log_coverage_is_reported_honestly():
    assert "Not searched: native Ollama server log" in build(can=canary(["native Ollama server log"]))
    assert "predates the not-searched check" in build(can=canary(...))


def test_self_contained_and_escaped():
    html = build()
    assert "<script" not in html.lower()  # also proves the file name was escaped
    assert "&lt;script&gt;" in html
    assert not re.search(r"(src|href)=['\"]?https?://", html)
    assert "@import" not in html


def test_small_sample_notes_are_merged_only_when_identical():
    notes = [f"{s}: only 20 samples; P99 is not reliable below 100." for s in ("pii_scan", "total")]
    assert combine_notes(notes) == ["only 20 samples per stage; P99 is not reliable below 100."]
    mixed = ["pii_scan: only 20 samples; P99 is not reliable below 100.",
             "total: only 19 samples; P99 is not reliable below 100."]
    assert combine_notes(mixed) == mixed


def test_canary_tiles_separate_detector_misses_from_downstream_leaks():
    html = build()
    assert "9/207" in html and "Canaries unmasked by the Trust Engine" in html
    assert "0/207" in html and "Canaries found in answers, logs, or storage" in html


def test_canary_split_counts_distinct_canaries_per_group():
    findings = [
        finding(1, "Ana", "response.masked_question"),
        finding(1, "Ana", "response.answer"),  # same canary, also echoed in the answer: counts in both
        finding(2, "021000021", "logs.backend", "US_ROUTING_NUMBER"),
        finding(2, "021000021", "database.t.c", "US_ROUTING_NUMBER"),  # same canary, two places: counts once
    ]
    can = canary([], findings)
    assert can["unmasked"] == {"total": 1, "by_entity": {"PERSON": 1}}
    assert can["downstream"] == {"total": 2, "by_entity": {"PERSON": 1, "US_ROUTING_NUMBER": 1}}


def test_headline_latency_uses_the_largest_run_and_says_so():
    # Batch has n=200 in its fixture; make the canary run bigger, then smaller.
    assert "n=500, canary audit batch canary-1" in build(can=canary([], latency_n=500))
    html = build(can=canary([], latency_n=20))
    assert "n=200, dashboard batch dashboard-20260101-000000" in html


def airgap(verdict="PASS", backend="docker"):
    return summarize_airgap(Path("airgap_check_1.json"), {
        "started_at": "t", "git_commit": "abc", "verdict": verdict, "model_backend": backend,
        "problems": [] if verdict == "PASS" else ["backend is on a network with an external route"], "notes": [],
        "checks": {
            "inventory": [{"service": "backend", "isolated": verdict == "PASS"}, {"service": "db", "isolated": True},
                          {"service": "proxy", "isolated": False}],
            "control_default_bridge": {"tcp_1.1.1.1:443": "CONNECTED"}, "proxy_egress": "CONNECTED",
            "functional_query": {"status": 200},
        },
    })


def test_isolation_claim_without_an_airgap_report_is_scoped():
    html = build()
    assert "Nothing leaves the machine" not in html
    assert "No external services are called" in html
    assert "enforced only in the all-Docker configuration" in html
    assert "native Ollama on the host, which is outside that isolation" in html  # batch fixture is native


def test_passing_airgap_check_is_cited_with_its_evidence():
    html = build(airgap=airgap())
    assert "airgap_check_1.json" in html and "verdict <b>PASS</b>" in html
    assert "(backend, db)" in html  # isolated data-handling containers; proxy listed separately
    assert "control" in html and "ingress proxy is the one container with a route out" in html
    assert "Air-gap check (Docker Ollama" in html  # tile


def test_failed_or_native_airgap_check_is_not_presented_as_isolation():
    for check in (airgap("FAIL"), airgap("PASS", backend="native")):
        html = build(airgap=check)
        assert "Isolation is not established" in html
        assert "network isolation is enforced by Docker" not in html
