"""Streamlit dashboard, organized by the buyer's questions.

Pages: Overview · Accuracy · Privacy · Isolation · Performance · Run a test · Report.
Every page gets the same Context (context.py): the selected reports, the
gateway's /info, and each headline number judged against acceptance.toml.

Talks only to the gateway over HTTP (no database credentials here) and reads
saved reports from REPORTS_DIR. Run in Compose: `docker compose up -d`, then
open http://localhost:8501.
"""

import os
from pathlib import Path

import streamlit as st

import reports
from batch import DEFAULT_API, DEFAULT_REPORTS_DIR, get_json
from context import KIND_LABELS, KINDS, load_context
from views import accuracy, isolation, overview, performance, privacy, report, run_test

API = os.environ.get("GATEWAY_URL", DEFAULT_API)
REPORTS_DIR = DEFAULT_REPORTS_DIR

st.set_page_config(page_title="AI Evaluation Sandbox", page_icon=":material/verified_user:", layout="wide")


def pick(container, kind: str, prefer: Path | None = None) -> Path | None:
    """Select box of saved reports of one kind, newest first. Starts on `reports.default_report`
    (most samples for canary audits, else newest), or on `prefer` right after a dashboard run."""
    files = reports.list_reports(REPORTS_DIR, kind)
    if not files:
        container.caption(f"{KIND_LABELS[kind]}: no reports yet")
        return None
    key = f"pick_{kind}"
    # A widget keeps its own state across reruns, so a new default must be set
    # in session state before the widget is created (e.g. right after a run).
    if prefer in files and st.session_state.pop(f"select_{kind}", False):
        st.session_state[key] = prefer
    if st.session_state.get(key) not in files:
        st.session_state[key] = reports.default_report(kind, files)
    return container.selectbox(KIND_LABELS[kind], files, format_func=lambda p: p.name, key=key)


info = get_json(API, "/info", timeout_s=5)
with st.sidebar:
    if info:
        st.success(f"Gateway connected · backend **{info['model_backend']}**", icon=":material/lan:")
        st.caption(
            f"LLM {info['llm_model']} via {info.get('llm_provider', 'ollama')}  \n"
            f"Embeddings {info['embed_model']} via {info.get('embed_provider', 'ollama')}  \n{info['scrubber']}"
        )
    else:
        st.error(f"Gateway not reachable at {API}", icon=":material/lan:")
    settings = st.expander("Settings: report sources", icon=":material/tune:")
    settings.caption("Which saved run each page uses. Defaults: newest, except the canary audit, which defaults to "
                     "the run with the most latency samples.")
    paths = {kind: pick(settings, kind, st.session_state.get("last_batch_path") if kind == "dashboard_batch" else None)
             for kind in KINDS}

ctx = load_context(REPORTS_DIR, API, info, paths)


def _overview():
    overview.render(ctx)


def _accuracy():
    accuracy.render(ctx)


def _privacy():
    privacy.render(ctx)


def _isolation():
    isolation.render(ctx)


def _performance():
    performance.render(ctx)


def _run_test():
    run_test.render(ctx)


def _report():
    report.render(ctx)


pages = {
    "overview": st.Page(_overview, title="Overview", icon=":material/dashboard:", default=True),
    "accuracy": st.Page(_accuracy, title="Accuracy", icon=":material/fact_check:", url_path="accuracy"),
    "privacy": st.Page(_privacy, title="Privacy", icon=":material/shield:", url_path="privacy"),
    "isolation": st.Page(_isolation, title="Isolation", icon=":material/wifi_off:", url_path="isolation"),
    "performance": st.Page(_performance, title="Performance", icon=":material/speed:", url_path="performance"),
    "run_test": st.Page(_run_test, title="Run a test", icon=":material/play_circle:", url_path="run"),
    "report": st.Page(_report, title="Report", icon=":material/download:", url_path="report"),
}
ctx.pages = pages
st.navigation({
    "Evaluation": [pages[k] for k in ("overview", "accuracy", "privacy", "isolation", "performance")],
    "Actions": [pages["run_test"], pages["report"]],
}).run()
