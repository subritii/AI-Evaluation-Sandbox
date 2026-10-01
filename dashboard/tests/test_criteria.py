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


ISOLATED_CAUSE = ("CPU-only inference on an 8 GB laptop; a production deployment would run a GPU inside the "
                  "isolated network.")


def test_shipped_config_has_the_confirmed_targets():
    cfg = criteria.load_config()
    by_id = {c.id: c for c in cfg.criteria}
    assert cfg.prepared_for == "Cobalt Harbor Bank"
    expected = {
        "answer_accuracy": (">=", 0.90), "detector_recall": (">=", 0.95), "canaries_unmasked": ("<=", 0),
        "canaries_downstream": ("<=", 0), "airgap": ("==", "PASS"),
        "pii_scan_p95_native": ("<=", 200), "end_to_end_p95_native": ("<=", 5000),
        "pii_scan_p95_isolated": ("<=", 200), "end_to_end_p95_isolated": ("<=", 5000),
    }
    assert {i: (by_id[i].comparison, by_id[i].target) for i in expected} == expected
    assert {c.question for c in cfg.criteria} == set(criteria.QUESTIONS)


def test_shipped_configurations_and_isolated_cause():
    cfg = criteria.load_config()
    native, isolated = cfg.configurations["native"], cfg.configurations["isolated"]
    assert (native.label, native.model_backend, native.headline) == ("Native GPU (development)", "native", True)
    assert (isolated.label, isolated.model_backend) == ("Isolated all-Docker CPU (8 GB laptop)", "docker")
    by_id = {c.id: c for c in cfg.criteria}
    assert by_id["pii_scan_p95_isolated"].known_gap == ISOLATED_CAUSE
    assert by_id["end_to_end_p95_isolated"].known_gap == ISOLATED_CAUSE
    assert not by_id["pii_scan_p95_native"].known_gap  # the development setup gets no excuse


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
    m = {"c": None if value is None else Measurement(value, "run")}
    [result] = criteria.evaluate(config_of(c), m)
    assert result.status == expected
    assert result.note == (known_gap if expected == KNOWN_GAP else "")


def test_passing_criterion_never_shows_its_known_gap_note():
    [r] = criteria.evaluate(config_of(crit(known_gap="Cause.")), {"c": Measurement(10, "run")})
    assert r.status == PASS and r.note == ""


def test_text_equality_and_formatting():
    c = crit(id="air", question="isolation", label="Air gap", metric="airgap_verdict", comparison="==",
             target="PASS", format="text")
    [ok] = criteria.evaluate(config_of(c), {"air": Measurement("PASS", "r")})
    [bad] = criteria.evaluate(config_of(c), {"air": Measurement("FAIL", "r")})
    assert (ok.status, bad.status) == (PASS, FAIL)
    assert ok.target_text == "= PASS"
    pct = crit(format="percent", comparison=">=", target=0.9, metric="answer_accuracy")
    [r] = criteria.evaluate(config_of(pct), {"c": Measurement(1.0, "r")})
    assert (r.value_text, r.target_text) == ("100.0%", "≥ 90.0%")
    cnt = crit(format="count_of_planted", target=0, metric="canaries_downstream")
    [r] = criteria.evaluate(config_of(cnt), {"c": Measurement(0, "r", 207)})
    assert (r.value_text, r.target_text) == ("0/207", "≤ 0")


def test_verdict_is_the_worst_status_per_question():
    results = criteria.evaluate(config_of(
        crit(id="a", question="privacy", metric="detector_recall", comparison=">=", target=0.95, format="percent",
             known_gap="Cause."),
        crit(id="b", question="privacy", metric="canaries_downstream", target=0, format="count_of_planted"),
    ), {"a": Measurement(0.9, "r"), "b": Measurement(0, "r", 207)})
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
    m = criteria.measure_all(ctx.config, ctx.summaries())
    assert m["answer_accuracy"].value == 1.0
    assert (m["canaries_unmasked"].value, m["canaries_unmasked"].denominator) == (3, 207)
    assert m["canaries_downstream"].value == 0
    assert m["airgap"].value == "PASS"
    # Each configuration uses its own largest run: native canary n=200 (not the n=20 native batch) ...
    assert m["pii_scan_p95_native"].value == 92.0 and m["pii_scan_p95_native"].run["n"] == 200
    # ... and the isolated row the Ollama-provider Docker run, not the newer OpenAI-compatible one.
    assert m["end_to_end_p95_isolated"].value == 26749.0
    assert m["end_to_end_p95_isolated"].run["run_id"] == "canary-20260929-213533"
    statuses = {r.criterion.id: r.status for r in ctx.results}
    assert statuses == {"answer_accuracy": PASS, "detector_recall": KNOWN_GAP, "canaries_unmasked": KNOWN_GAP,
                        "canaries_downstream": PASS, "airgap": PASS,
                        "pii_scan_p95_native": PASS, "end_to_end_p95_native": PASS,
                        "pii_scan_p95_isolated": KNOWN_GAP, "end_to_end_p95_isolated": KNOWN_GAP}
    isolated = next(r for r in ctx.results if r.criterion.id == "end_to_end_p95_isolated")
    assert isolated.label == "End-to-end P95 · Isolated all-Docker CPU (8 GB laptop)"
    assert isolated.note == ISOLATED_CAUSE


def test_no_reports_means_no_data_everywhere(tmp_path: Path):
    empty = tmp_path / "reports"
    empty.mkdir()
    ctx = load_context(empty, "http://127.0.0.1:9", None, default_paths(empty))
    assert {v.status for v in ctx.verdicts} == {NO_DATA}


def test_eval_load_location_is_reported_not_assumed():
    import reports
    from tests.fixtures import eval_rag
    old = reports.summarize_eval_rag(Path("eval_rag_1.json"), eval_rag())
    new_report = eval_rag()
    new_report["environment"]["load_measured_in"] = "docker_vm"
    new = reports.summarize_eval_rag(Path("eval_rag_2.json"), new_report)
    assert (old["load_where"], new["load_where"]) == ("where not recorded", "Docker VM")


def run(backend, n, providers=("ollama",), name="r"):
    return {"run_id": name, "n": n, "model_backends": [backend], "providers": sorted(providers)}


def test_configuration_run_picks_largest_matching_run_newest_on_tie():
    conf = criteria.Configuration("isolated", "Isolated", "docker")
    runs = [run("docker", 20, name="newest"), run("docker", 20, name="older"), run("native", 200),
            run("docker", 50, ("openai_compatible", "ollama"), "other-provider")]
    assert criteria.configuration_run(runs, conf)["run_id"] == "newest"
    runs.append(run("docker", 21, name="bigger"))
    assert criteria.configuration_run(runs, conf)["run_id"] == "bigger"
    assert criteria.configuration_run([run("native", 200)], conf) is None


def test_mixed_backend_run_belongs_to_no_configuration():
    conf = criteria.Configuration("isolated", "Isolated", "docker")
    mixed = {"run_id": "m", "n": 99, "model_backends": ["docker", "native"], "providers": ["ollama"]}
    assert not criteria.run_matches(mixed, conf)


def test_unknown_configuration_is_an_error(tmp_path: Path):
    path = tmp_path / "a.toml"
    path.write_text('[[configurations]]\nid = "native"\nlabel = "N"\nmodel_backend = "native"\n'
                    '[[criteria]]\nid = "x"\nquestion = "performance"\nlabel = "x"\nmetric = "pii_scan_p95_ms"\n'
                    'comparison = "<="\ntarget = 1\nformat = "ms"\nconfiguration = "gpu-cluster"\n')
    with pytest.raises(ValueError, match="unknown configuration"):
        criteria.load_config(path)


def test_only_latency_metrics_can_be_scoped(tmp_path: Path):
    path = tmp_path / "a.toml"
    path.write_text('[[configurations]]\nid = "native"\nlabel = "N"\nmodel_backend = "native"\n'
                    '[[criteria]]\nid = "x"\nquestion = "accuracy"\nlabel = "x"\nmetric = "answer_accuracy"\n'
                    'comparison = ">="\ntarget = 1\nformat = "percent"\nconfiguration = "native"\n')
    with pytest.raises(ValueError, match="only latency metrics"):
        criteria.load_config(path)
