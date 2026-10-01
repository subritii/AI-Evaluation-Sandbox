"""Realistic saved reports of every kind, written to a temp reports/ directory for page tests."""

import json
from pathlib import Path

STAGES = ("pii_scan", "retrieval", "time_to_first_token", "generation", "total")


def _stages(n: int, pii_p95: float, total_p95: float) -> dict:
    out = {}
    for s in STAGES:
        p95 = pii_p95 if s == "pii_scan" else (total_p95 if s == "total" else total_p95 / 3)
        out[s] = {"n": n, "avg": p95 * 0.6, "p50": p95 * 0.5, "p90": p95 * 0.9, "p95": p95, "p99": p95 * 1.2,
                  "min": 1.0, "max": p95 * 1.5}
    return out


def _case(qid, question, refuse, answer_pass, answer, excerpt=True):
    retrieved = [{"ref": f"policy.md#{i}", "similarity": 0.8 - i / 10, "heading": f"## {i}. Section",
                  **({"excerpt": f"Excerpt of chunk {i}."} if excerpt else {})} for i in range(2)]
    return {
        "id": qid, "question": question, "should_refuse": refuse, "retrieved": retrieved,
        "expected_rank": None if refuse else 1, "retrieval_hit": None if refuse else True,
        "answer": answer, "answer_pass": answer_pass, "missing_keywords": [] if answer_pass else [["delete"]],
        "refused": refuse, "citations": [] if refuse else [{"ref": "policy.md#0", "section": "0. Section",
                                                          "support": 3.5, "has_evidence": True}],
        "citation_pass": True, "citation_precision": None if refuse else 1.0,
        "timings_ms": {"pii_scan": 40.0, "retrieval": 60.0, "time_to_first_token": 900.0, "generation": 1100.0,
                       "total": 2100.0},
    }


def eval_rag(answer_passes=(True, True), excerpt=True, commit="abc1234", backend="native"):
    return {
        "started_at": "2026-09-29T19:59:59+00:00", "git_commit": commit,
        "config": {"llm_model": "llama3.2:3b", "embed_model": "nomic-embed-text", "model_backend": backend},
        "environment": {"host_load_avg_before": [3.0, 1, 1], "host_load_avg_after": [4.0, 1, 1]},
        "results": [
            _case("kyc_retention", "How long are KYC records kept?", False, answer_passes[0],
                  "KYC records are kept for five years.", excerpt),
            _case("cvv_deletion", "When must a CVV be deleted?", False, answer_passes[1],
                  "CVVs must never be stored after authorization.", excerpt),
            _case("dress_code", "What is the bank's dress code?", True, True, "I don't know.", excerpt),
        ],
    }


def write_all(reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)

    def put(name, data):
        (reports_dir / name).write_text(json.dumps(data))

    put("eval_rag_20260929-195959.json", eval_rag())
    # Same name as the baseline in acceptance.toml, so the before/after section loads.
    put("eval_rag_20260929-153914.json", eval_rag(answer_passes=(True, False), excerpt=False, commit="23ba6b1", backend=None))
    put("eval_detector_20260929-212122.json", {
        "started_at": "2026-09-29T21:21:22+00:00", "git_commit": "1f9e6cc", "scrubber": "presidio-x+rules-v2",
        "score_threshold": 0.4, "dataset": {"records": 1000, "sha256": "ab" * 32},
        "per_entity": {"PERSON": {"support": 10, "precision": 1.0, "recall": 0.9, "tp": 9, "fp": 0, "fn": 1}},
        "overall_micro": {"support": 10, "precision": 1.0, "recall": 0.9, "tp": 9, "fp": 0, "fn": 1},
        "by_difficulty": {"standard": {"recall": 0.97, "precision": 1.0, "support": 8}},
        "negative_records_flagged": {"flagged": 3, "total": 150},
    })
    findings = [{"request": i, "value_synthetic": f"0210000{i:02d}", "location": "response.masked_question",
                 "entity_type": "US_ROUTING_NUMBER"} for i in range(3)]
    models = [{"model_backend": "native", "llm_model": "llama3.2:3b", "embed_model": "nomic-embed-text",
               "llm_provider": "ollama", "embed_provider": "ollama", "requests": 200}]
    put("canary_audit_20260929-203656.json", {
        "run_id": "canary-20260929-203656", "started_at": "2026-09-29T20:36:56+00:00", "git_commit": "cbdcc41",
        "config": {"requests": 200}, "environment": {"host_load_avg_before": [2.2, 1, 1], "host_load_avg_after": [4.1, 1, 1]},
        "model_config": models,
        "latency": {"run_id": "canary-20260929-203656", "models": models, "stages": _stages(200, 92.0, 3341.0), "notes": []},
        "planted": {"total": 207, "by_entity": {"US_ROUTING_NUMBER": 37, "PERSON": 51}},
        "found": {"total": 3, "by_entity": {"US_ROUTING_NUMBER": 3},
                  "by_location": {"response.masked_question": {"US_ROUTING_NUMBER": 3}}},
        "searched_locations": {"responses": ["masked_question", "answer"], "logs": ["backend"], "database_columns": ["t.c"]},
        "not_searched": [], "findings": findings,
    })
    # The isolated all-Docker CPU configuration: smaller and much slower.
    docker_models = [{"model_backend": "docker", "llm_model": "llama3.2:3b", "embed_model": "nomic-embed-text",
                      "requests": 20}]  # no provider fields: recorded before Task 9, so Ollama
    put("canary_audit_20260929-213533.json", {
        "run_id": "canary-20260929-213533", "started_at": "2026-09-29T21:35:33+00:00", "git_commit": "2ef114f",
        "config": {"requests": 20}, "environment": {"host_load_avg_before": [28.6, 1, 1], "host_load_avg_after": [17.4, 1, 1]},
        "model_config": docker_models,
        "latency": {"run_id": "canary-20260929-213533", "models": docker_models, "stages": _stages(20, 364.0, 26749.0), "notes": []},
        "planted": {"total": 21, "by_entity": {"PERSON": 5}}, "found": {"total": 0, "by_entity": {}, "by_location": {}},
        "searched_locations": {}, "not_searched": [], "findings": [],
    })
    # All-Docker but through the OpenAI-compatible API: not the isolated configuration as defined (providers differ).
    openai_models = [dict(docker_models[0], llm_provider="openai_compatible", embed_provider="openai_compatible")]
    put("canary_audit_20260930-161023.json", {
        "run_id": "canary-20260930-161023", "started_at": "2026-09-30T16:10:23+00:00", "git_commit": "0682c2f",
        "config": {"requests": 20}, "environment": {"host_load_avg_before": [12.0, 1, 1]},
        "model_config": openai_models,
        "latency": {"run_id": "canary-20260930-161023", "models": openai_models, "stages": _stages(20, 224.0, 16329.0), "notes": []},
        "planted": {"total": 21, "by_entity": {"PERSON": 5}}, "found": {"total": 0, "by_entity": {}, "by_location": {}},
        "searched_locations": {}, "not_searched": [], "findings": [],
    })
    put("airgap_check_20260929-214335.json", {
        "kind": "airgap_check", "started_at": "2026-09-29T21:43:35+00:00", "git_commit": "23579ac", "verdict": "PASS",
        "problems": [], "notes": ["The ingress proxy can reach the internet."], "model_backend": "docker",
        "checks": {
            "inventory": [
                {"service": "backend", "isolated": True, "networks": {"ai-eval-sandbox_sandbox": "internal"}, "published_ports": []},
                {"service": "proxy", "isolated": False, "networks": {"ai-eval-sandbox_edge": "external route"},
                 "published_ports": ["127.0.0.1:8501->8501/tcp"]},
            ],
            "in_container": {"backend": {"tcp_1.1.1.1:443": "blocked: OSError"}},
            "sandbox_network": {"tcp_1.1.1.1:443": "blocked: OSError"},
            "control_default_bridge": {"tcp_1.1.1.1:443": "CONNECTED"},
            "internal_reachability": {"db:5432": "reachable"}, "ingress": {"api": "HTTP 200"},
            "proxy_egress": "CONNECTED", "functional_query": {"status": 200},
        },
    })
    put("dashboard_batch_20260929-210005.json", {
        "kind": "dashboard_batch", "run_id": "dashboard-20260929-210005", "started_at": "2026-09-29T21:00:05+00:00",
        "input": {"file_name": "policy_questions.csv", "sha256": "cd" * 32, "questions": 2, "warmup": 2},
        "gateway": {"model_backend": "native", "llm_model": "llama3.2:3b", "embed_model": "nomic-embed-text"},
        "environment": {"gateway_load_avg_before": [0.5, 1, 1], "gateway_load_avg_after": [0.6, 1, 1], "gateway_cpus": 8},
        "status_counts": {"200": 2}, "masked_entity_totals": {"PERSON": 1}, "refused": 0,
        "latency": {"stages": _stages(20, 101.0, 4395.0), "notes": []},
        "requests": [{"index": i, "status": 200, "masked_entities": {}, "refused": False, "citations": 1, "error": None,
                      "timings_ms": {"pii_scan": 50.0, "total": 2000.0}} for i in range(2)],
    })
    return reports_dir
