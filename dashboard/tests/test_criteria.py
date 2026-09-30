"""Acceptance criteria: config validation, status rules, measurements, and per-question verdicts."""

from pathlib import Path

import pytest

import criteria
from context import default_paths, load_context
from criteria import FAIL, KNOWN_GAP, NO_DATA, PASS, Criterion, Measurement
from tests import fixtures


def config_of(*items: Criterion) -> criteria.Config:
    return criteria.Config("Test Bank", list(items))


def crit(**kw) -> Criterion:
    base = dict(id="c", question="performance", label="PII scan P95", metric="pii_scan_p95_ms",
                comparison="<=", target=200, format="ms")
    return Criterion(**(base | kw))


def test_shipped_config_loads_with_the_owner_targets():
    cfg = criteria.load_config()
    by_id = {c.id: c for c in cfg.criteria}
    assert cfg.prepared_for == "Cobalt Harbor Bank"
    assert (by_id["pii_scan_p95"].comparison, by_id["pii_scan_p95"].target) == ("<=", 200)
    assert (by_id["answer_accuracy"].comparison, by_id["answer_accuracy"].target) == (">=", 0.90)
    assert (by_id["canaries_downstream"].comparison, by_id["canaries_downstream"].target) == ("<=", 0)
    assert (by_id["airgap"].comparison, by_id["airgap"].target) == ("==", "PASS")
    assert {c.question for c in cfg.criteria} == set(criteria.QUESTIONS)


@pytest.mark.parametrize(("value", "known_gap", "expected"), [
    (150, "", PASS),
    (200, "", PASS),  # the target itself passes (<=)
    (250, "", FAIL),  # a miss with no documented cause is a fail
    (250, "   ", FAIL),  # whitespace is not a documented cause
    (250, "Documented cause.", KNOWN_GAP),
    (None, "Documented cause.", NO_DATA),
])
def test_status_rules(value, known_gap, expected):
    c = crit(known_gap=known_gap)
    m = {"pii_scan_p95_ms": None if value is None else Measurement(value, "run")}
    [result] = criteria.evaluate(config_of(c), m)
    assert result.status == expected
    assert result.note == (known_gap if expected == KNOWN_GAP else "")


def test_passing_criterion_never_shows_its_known_gap_note():
    [r] = criteria.evaluate(config_of(crit(known_gap="Cause.")), {"pii_scan_p95_ms": Measurement(10, "run")})
    assert r.status == PASS and r.note == ""


def test_text_equality_and_formatting():
    c = crit(id="air", question="isolation", label="Air gap", metric="airgap_verdict", comparison="==",
             target="PASS", format="text")
    [ok] = criteria.evaluate(config_of(c), {"airgap_verdict": Measurement("PASS", "r")})
    [bad] = criteria.evaluate(config_of(c), {"airgap_verdict": Measurement("FAIL", "r")})
    assert (ok.status, bad.status) == (PASS, FAIL)
    assert ok.target_text == "= PASS"
    pct = crit(format="percent", comparison=">=", target=0.9, metric="answer_accuracy")
    [r] = criteria.evaluate(config_of(pct), {"answer_accuracy": Measurement(1.0, "r")})
    assert (r.value_text, r.target_text) == ("100.0%", "≥ 90.0%")
    cnt = crit(format="count_of_planted", target=0, metric="canaries_downstream")
    [r] = criteria.evaluate(config_of(cnt), {"canaries_downstream": Measurement(0, "r", 207)})
    assert (r.value_text, r.target_text) == ("0/207", "≤ 0")


def test_verdict_is_the_worst_status_per_question():
    results = criteria.evaluate(config_of(
        crit(id="a", question="privacy", metric="detector_recall", comparison=">=", target=0.95, format="percent",
             known_gap="Cause."),
        crit(id="b", question="privacy", metric="canaries_downstream", target=0, format="count_of_planted"),
    ), {"detector_recall": Measurement(0.9, "r"), "canaries_downstream": Measurement(0, "r", 207)})
    verdicts = {v.question: v for v in criteria.verdicts(results)}
    assert verdicts["privacy"].status == KNOWN_GAP
    assert verdicts["accuracy"].status == NO_DATA  # no criteria for it in this config
    assert criteria.worst([PASS, KNOWN_GAP, NO_DATA, FAIL]) == FAIL
    assert criteria.worst([PASS, KNOWN_GAP]) == KNOWN_GAP


@pytest.mark.parametrize(("line", "message"), [
    ('metric = "nope"', "unknown metric"),
    ('comparison = "<"', "unknown comparison"),
    ('question = "cost"', "unknown question"),
    ('format = "hours"', "unknown format"),
])
def test_malformed_config_is_an_error(tmp_path: Path, line, message):
    fields = {"metric": '"pii_scan_p95_ms"', "comparison": '"<="', "question": '"performance"', "format": '"ms"'}
    key = line.split(" = ")[0]
    fields[key] = line.split(" = ")[1]
    body = "\n".join(f"{k} = {v}" for k, v in fields.items())
    path = tmp_path / "a.toml"
    path.write_text(f'[[criteria]]\nid = "x"\nlabel = "x"\ntarget = 1\n{body}\n')
    with pytest.raises(ValueError, match=message):
        criteria.load_config(path)


def test_duplicate_ids_are_an_error(tmp_path: Path):
    one = 'id = "x"\nquestion = "performance"\nlabel = "x"\nmetric = "pii_scan_p95_ms"\ncomparison = "<="\ntarget = 1\nformat = "ms"\n'
    path = tmp_path / "a.toml"
    path.write_text(f"[[criteria]]\n{one}[[criteria]]\n{one}")
    with pytest.raises(ValueError, match="unique"):
        criteria.load_config(path)


def test_measurements_from_fixture_reports(tmp_path: Path):
    reports_dir = fixtures.write_all(tmp_path / "reports")
    ctx = load_context(reports_dir, "http://127.0.0.1:9", None, default_paths(reports_dir))
    m = criteria.measure_all(ctx.summaries())
    assert m["answer_accuracy"].value == 1.0
    assert (m["canaries_unmasked"].value, m["canaries_unmasked"].denominator) == (3, 207)
    assert m["canaries_downstream"].value == 0
    assert m["airgap_verdict"].value == "PASS"
    # Headline latency uses the larger run (canary n=200) over the dashboard batch (n=20).
    assert m["pii_scan_p95_ms"].value == 92.0 and "n=200" in m["pii_scan_p95_ms"].source
    statuses = {r.criterion.id: r.status for r in ctx.results}
    assert statuses == {"answer_accuracy": PASS, "detector_recall": KNOWN_GAP, "canaries_unmasked": KNOWN_GAP,
                        "canaries_downstream": PASS, "airgap": PASS, "pii_scan_p95": PASS, "end_to_end_p95": PASS}


def test_no_reports_means_no_data_everywhere(tmp_path: Path):
    empty = tmp_path / "reports"
    empty.mkdir()
    ctx = load_context(empty, "http://127.0.0.1:9", None, default_paths(empty))
    assert {v.status for v in ctx.verdicts} == {NO_DATA}
