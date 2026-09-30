"""Every page renders against realistic saved reports, with no gateway reachable (as in CI)."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import batch
from tests import fixtures

HARNESS = str(Path(__file__).with_name("page_harness.py"))
APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture
def reports_dir(tmp_path: Path) -> Path:
    return fixtures.write_all(tmp_path / "reports")


def render(monkeypatch, reports_dir: Path, page: str, trace: str | None = None) -> AppTest:
    monkeypatch.setenv("HARNESS_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("HARNESS_PAGE", page)
    if trace:
        monkeypatch.setenv("HARNESS_TRACE", trace)
    at = AppTest.from_file(HARNESS, default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def text_of(at: AppTest) -> str:
    parts = [e.value for kind in ("title", "markdown", "caption", "info", "warning", "error", "subheader")
             for e in getattr(at, kind)]
    parts += [str(m.value) + " " + m.label for m in at.metric]
    return "\n".join(str(p) for p in parts)


@pytest.mark.parametrize("page", ["overview", "accuracy", "privacy", "isolation", "performance", "run_test", "report"])
def test_every_page_renders(monkeypatch, reports_dir, page):
    render(monkeypatch, reports_dir, page)


def test_overview_has_a_verdict_per_question_and_names_the_runs(monkeypatch, reports_dir):
    text = text_of(render(monkeypatch, reports_dir, "overview"))
    assert "Prepared for Cobalt Harbor Bank" in text
    for title in ("DOES IT ANSWER CORRECTLY?", "DOES PII STAY PROTECTED?", "IS IT CUT OFF FROM THE INTERNET?",
                  "IS IT FAST ENOUGH?"):
        assert title in text
    assert "Canary audit: 200 requests" in text and "RAG eval: 3 questions" in text
    assert "None of the 207 planted fake PII values reached answers, logs, or storage." in text
    assert "(target ≤ 200 ms)" in text  # headline numbers shown against their targets
    assert "different model backends" in text  # native evals vs all-Docker air-gap check


def test_accuracy_trace_shows_chunks_answer_citations_and_timings(monkeypatch, reports_dir):
    at = render(monkeypatch, reports_dir, "accuracy", trace="kyc_retention")
    text = text_of(at)
    assert "Trace: `kyc_retention`" in text
    assert "How long are KYC records kept?" in text  # synthetic eval question, allowed on screen
    assert "KYC records are kept for five years." in text
    assert "Per-stage timings" in text and "2,100 ms" in text
    tables = [d.value for d in at.dataframe]
    assert any("excerpt" in t.columns and "Excerpt of chunk 0." in t["excerpt"].tolist() for t in tables)
    assert any("holds the evidence" in t.columns for t in tables)


def test_accuracy_before_after_uses_the_configured_baseline(monkeypatch, reports_dir):
    at = render(monkeypatch, reports_dir, "accuracy")
    text = text_of(at)
    assert "1/2 Baseline: answers correct" in text and "2/2 Current: answers correct" in text
    change = next(d.value for d in at.dataframe if "change" in d.value.columns)
    assert change.set_index("question").loc["cvv_deletion", "change"] == "fixed"


def test_privacy_splits_unmasked_from_downstream(monkeypatch, reports_dir):
    text = text_of(render(monkeypatch, reports_dir, "privacy"))
    assert "0/207 Found in answers, logs, or storage" in text
    assert "3/207 Unmasked by the Trust Engine" in text


def test_performance_headline_is_the_largest_run(monkeypatch, reports_dir):
    text = text_of(render(monkeypatch, reports_dir, "performance"))
    assert "n=200, canary audit batch canary-20260929-203656" in text
    assert "92 ms PII scan P95" in text


def test_run_test_page_shows_no_question_text_from_saved_runs(monkeypatch, reports_dir):
    at = render(monkeypatch, reports_dir, "run_test")
    assert at.button[0].disabled  # no gateway: the run button is off
    for df in (d.value for d in at.dataframe):
        assert not any("question" in c for c in df.columns)


def test_full_app_renders_with_navigation_and_settings(monkeypatch, reports_dir):
    monkeypatch.setattr(batch, "DEFAULT_REPORTS_DIR", reports_dir)
    monkeypatch.setenv("GATEWAY_URL", "http://127.0.0.1:9")
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title[0].value == "Overview"
    picked = {s.label: s.value.name for s in at.selectbox}
    assert picked["Canary audit"] == "canary_audit_20260929-203656.json"
    assert picked["RAG eval"] == "eval_rag_20260929-195959.json"  # newest; the baseline is older
