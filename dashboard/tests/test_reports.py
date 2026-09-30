"""Which saved report the dashboard selects by default."""

import json

from reports import default_report, list_reports


def write(tmp_path, name, n):
    latency = {"stages": {"total": {"n": n}}} if n else None
    (tmp_path / name).write_text(json.dumps({"latency": latency}))
    return tmp_path / name


def test_canary_default_is_the_run_with_most_samples_not_the_newest(tmp_path):
    write(tmp_path, "canary_audit_20260929-200203.json", 200)
    write(tmp_path, "canary_audit_20260929-203656.json", 200)  # newer, same size: wins the tie
    write(tmp_path, "canary_audit_20260929-213533.json", 20)  # newest, but small
    write(tmp_path, "canary_audit_20260929-163437.json", 0)  # no latency recorded
    files = list_reports(tmp_path, "canary_audit")
    assert default_report("canary_audit", files).name == "canary_audit_20260929-203656.json"


def test_other_kinds_default_to_newest(tmp_path):
    write(tmp_path, "eval_rag_20260929-195959.json", 11)
    write(tmp_path, "eval_rag_20260929-213200.json", 5)
    assert default_report("eval_rag", list_reports(tmp_path, "eval_rag")).name == "eval_rag_20260929-213200.json"


def test_unreadable_report_counts_as_zero_samples(tmp_path):
    write(tmp_path, "canary_audit_20260929-200203.json", 200)
    (tmp_path / "canary_audit_20260929-213533.json").write_text("{not json")
    files = list_reports(tmp_path, "canary_audit")
    assert default_report("canary_audit", files).name == "canary_audit_20260929-200203.json"
    assert default_report("canary_audit", []) is None
