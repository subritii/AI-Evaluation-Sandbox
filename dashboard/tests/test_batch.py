"""Batch parsing and run records. No gateway needed: HTTP is faked with httpx.MockTransport."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from batch import MAX_ROWS, BatchError, RequestResult, build_run_record, parse_batch, send_one

SECRET = "Maria Lopez 536-22-1847"  # synthetic; stands in for PII that must never be echoed


def test_parses_csv_with_bom_and_extra_columns():
    data = "﻿Question,note\nHow long are KYC records kept?,synthetic\n\"Commas, quoted\",x\n".encode()
    assert parse_batch("batch.csv", data) == ["How long are KYC records kept?", "Commas, quoted"]


def test_parses_jsonl_and_skips_blank_lines():
    data = b'{"question": "Q1"}\n\n{"question": "Q2", "note": "synthetic"}\n'
    assert parse_batch("batch.jsonl", data) == ["Q1", "Q2"]


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("batch.txt", b"question\nQ1\n", ".csv or .jsonl"),
        ("batch.csv", b"text\nQ1\n", "`question` column"),
        ("batch.csv", b"question\n", "no questions"),
        ("batch.jsonl", b'{"question": "Q1"}\nnot json\n', "Line 2"),
        ("batch.jsonl", b'{"q": "Q1"}\n', "Line 1"),
        ("batch.csv", b"question\n\"   \"\n", "Question 1 is empty"),
        ("batch.csv", b"\xff\xfe", "UTF-8"),
    ],
)
def test_rejects_bad_files_with_a_clear_message(name, data, message):
    with pytest.raises(BatchError, match=message):
        parse_batch(name, data)


def test_error_messages_never_echo_row_content():
    bad_json = f'{{"question": "ok"}}\n{{"question": "{SECRET}"'.encode()
    too_long = ("question\n" + SECRET + "x" * 4000 + "\n").encode()
    for name, data in (("b.jsonl", bad_json), ("b.csv", too_long)):
        with pytest.raises(BatchError) as exc:
            parse_batch(name, data)
        assert "Maria" not in str(exc.value) and "536-22" not in str(exc.value)


def test_row_limit():
    data = ("question\n" + "Q\n" * (MAX_ROWS + 1)).encode()
    with pytest.raises(BatchError, match=f"limit is {MAX_ROWS}"):
        parse_batch("b.csv", data)


def _client(handler) -> httpx.Client:
    return httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handler))


def test_send_one_keeps_only_post_masking_fields():
    def handler(request):
        assert json.loads(request.content)["run_id"] == "run-1"
        return httpx.Response(200, json={
            "request_id": "x", "run_id": "run-1", "masked_question": "I'm [PERSON_1].",
            "masked_entities": {"PERSON": 1}, "answer": "...", "refused": False,
            "citations": [{"source": "p.md"}], "timings_ms": {"pii_scan": 30.0, "total": 900.0},
        })

    r = send_one(_client(handler), 0, SECRET, "run-1")
    assert r.status == 200 and r.masked_question == "I'm [PERSON_1]." and r.citations == 1
    assert r.answer == "..." and r.citation_detail == [{"source": "p.md"}]  # kept in memory for the session trace
    assert SECRET not in json.dumps(r.__dict__)


def test_send_one_records_errors_without_the_body():
    r = send_one(_client(lambda req: httpx.Response(502, json={"detail": "Model server error"})), 3, SECRET, "run")
    assert (r.status, r.error, r.masked_question) == (502, "Model server error", None)

    def boom(request):
        raise httpx.ConnectError("refused")

    r = send_one(_client(boom), 4, SECRET, "run")
    assert r.status is None and r.error == "ConnectError"


def test_run_record_stores_no_question_text_even_masked():
    # Masking can miss PII (canary audit), so masked text stays out of saved runs.
    results = [
        RequestResult(0, 200, masked_question=f"leaked {SECRET}", masked_entities={"PERSON": 1},
                      refused=False, citations=2, timings_ms={"total": 1000.0},
                      answer=f"Echoed {SECRET}", citation_detail=[{"source": "p.md", "section": "KYC"}]),
        RequestResult(1, 502, error="Model server error"),
    ]
    record = build_run_record(
        run_id="dashboard-20260101-000000", started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        file_name="b.csv", file_hash="ab" * 32, warmup=2, results=results,
        gateway_before={"model_backend": "native", "load_avg": [1.0, 1.0, 1.0], "cpus": 8},
        gateway_after={"load_avg": [2.0, 1.0, 1.0]}, metrics=None, wall_time_s=12.34,
    )
    text = json.dumps(record)
    assert "leaked" not in text and "Maria" not in text and "Echoed" not in text and "citation_detail" not in text
    assert record["status_counts"] == {"200": 1, "502": 1}
    assert record["masked_entity_totals"] == {"PERSON": 1}
    assert record["environment"]["gateway_load_avg_after"] == [2.0, 1.0, 1.0]
