"""Streamlit dashboard: upload a batch, watch per-stage latency live, review results, download a report.

Talks only to the gateway over HTTP (no database credentials here) and reads
saved reports from REPORTS_DIR. Run in Compose: `docker compose up -d dashboard`,
then open http://localhost:8501.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

import reports
from batch import DEFAULT_API, DEFAULT_REPORTS_DIR, MAX_ROWS, STAGES, BatchError, RequestResult, execute, file_sha256, get_json, parse_batch
from report_html import STAGE_NOTES, build_report, combine_notes

API = os.environ.get("GATEWAY_URL", DEFAULT_API)
REPORTS_DIR = DEFAULT_REPORTS_DIR

st.set_page_config(page_title="AI Evaluation Sandbox", layout="wide")


# --- Sidebar: gateway status and which saved reports to use ---------------------

def pick(kind: str, label: str, prefer: Path | None = None) -> Path | None:
    """Select box of saved reports of one kind, newest first (or `prefer`, e.g. the run just made)."""
    files = reports.list_reports(REPORTS_DIR, kind)
    if not files:
        st.sidebar.caption(f"{label}: no reports yet")
        return None
    key = f"pick_{kind}"
    # A widget keeps its own state across reruns, so a new default must be set
    # in session state before the widget is created (e.g. right after a run).
    if prefer in files and st.session_state.pop(f"select_{kind}", False):
        st.session_state[key] = prefer
    if st.session_state.get(key) not in files:
        st.session_state[key] = files[0]
    return st.sidebar.selectbox(label, files, format_func=lambda p: p.name, key=key)


st.sidebar.header("Gateway")
info = get_json(API, "/info", timeout_s=5)
if info:
    st.sidebar.success(f"Connected · backend **{info['model_backend']}**")
    st.sidebar.caption(f"{info['llm_model']} · {info['embed_model']}  \n{info['scrubber']}")
else:
    st.sidebar.error(f"Gateway not reachable at {API}")

st.sidebar.header("Report sources")
st.sidebar.caption("Newest first. These feed the Results tab and the report.")
batch_path = pick("dashboard_batch", "Batch run", st.session_state.get("last_batch_path"))
rag_path = pick("eval_rag", "RAG eval")
det_path = pick("eval_detector", "Detector eval")
can_path = pick("canary_audit", "Canary audit")


@st.cache_data(show_spinner=False)
def load_summaries(rag: Path | None, det: Path | None, can: Path | None) -> tuple:
    """Load and summarize the selected reports (cached per file selection)."""
    rag_s = reports.summarize_eval_rag(rag, reports.load(rag)) if rag else None
    det_s = reports.summarize_detector(det, reports.load(det)) if det else None
    can_s = reports.summarize_canary(can, reports.load(can)) if can else None
    return rag_s, det_s, can_s


rag_s, det_s, can_s = load_summaries(rag_path, det_path, can_path)
batch_record = reports.load(batch_path) if batch_path else None

st.title("AI Evaluation Sandbox")
tab_run, tab_results, tab_report = st.tabs(["1 · Run a batch", "2 · Results", "3 · Report"])


# --- Tab 1: upload and run ----------------------------------------------------

def timing_points(r: dict) -> list[dict]:
    """Chart rows for one request: (request #, stage, ms). Log scale needs ms > 0."""
    return [{"request": r["index"] + 1, "stage": s, "ms": max(r["timings_ms"][s], 0.01)}
            for s in STAGES if s in r["timings_ms"]]


def latency_chart(rows: list[dict]) -> alt.Chart:
    """Per-request stage timings. Log scale: pii_scan (~50 ms) and generation (~seconds) on one chart."""
    df = pd.DataFrame(rows, columns=["request", "stage", "ms"])
    return (
        alt.Chart(df)
        .mark_line(point=alt.OverlayMarkDef(size=18))
        .encode(
            x=alt.X("request:Q", title="Request #", axis=alt.Axis(format="d", tickMinStep=1)),
            y=alt.Y("ms:Q", title="ms (log scale)", scale=alt.Scale(type="log")),
            color=alt.Color("stage:N", sort=list(STAGES), title="Stage"),
            tooltip=["request", "stage", alt.Tooltip("ms:Q", format=",.0f")],
        )
        .properties(height=340)
    )


with tab_run:
    st.subheader("Upload a question batch")
    st.caption(
        f"CSV with a `question` column, or JSONL with one `{{\"question\": ...}}` per line; up to {MAX_ROWS} questions. "
        "The file stays in memory: it is never saved, logged, or shown. Only masked results are kept."
    )
    upload = st.file_uploader("Batch file", type=["csv", "jsonl"])
    warmup = st.number_input("Warmup requests (excluded from metrics)", min_value=0, max_value=10, value=2)

    questions = None
    if upload is not None:
        data = upload.getvalue()
        try:
            questions = parse_batch(upload.name, data)
            st.info(f"**{len(questions)} questions** · sha256 `{file_sha256(data)[:16]}…`")
        except BatchError as exc:
            st.error(str(exc))

    run = st.button("Run batch", type="primary", disabled=not (questions and info))
    if run and questions:
        progress = st.progress(0.0, text="Warming up…")
        chart_slot = st.empty()
        table_slot = st.empty()
        points: list[dict] = []
        shown: list[dict] = []

        def on_result(r: RequestResult) -> None:
            points.extend(timing_points({"index": r.index, "timings_ms": r.timings_ms}))
            shown.append({
                "#": r.index + 1,
                "status": r.status,
                "masked question (as the models saw it)": r.masked_question or r.error,
                "entities masked": ", ".join(f"{k} {v}" for k, v in r.masked_entities.items()),
                "total ms": round(r.timings_ms.get("total", 0)),
            })
            progress.progress((r.index + 1) / len(questions), text=f"{r.index + 1}/{len(questions)} requests")
            if points:
                chart_slot.altair_chart(latency_chart(points), width="stretch")
            table_slot.dataframe(pd.DataFrame(shown[-15:]), hide_index=True, width="stretch")

        try:
            record, path = execute(upload.name, upload.getvalue(), API, int(warmup), REPORTS_DIR, on_result=on_result)
        except (BatchError, ConnectionError) as exc:
            st.error(str(exc))
        else:
            st.session_state["last_batch_path"] = path
            st.session_state["select_dashboard_batch"] = True
            st.session_state["last_masked_rows"] = shown
            st.success(f"Done: run `{record['run_id']}` saved to `reports/{path.name}`. Open **3 · Report** to download.")
            st.rerun()  # refresh the sidebar so the new run is selected

    if st.session_state.get("last_masked_rows") and batch_path == st.session_state.get("last_batch_path"):
        with st.expander("Masked questions from the last run (this session only, not saved)"):
            st.caption("Masking can miss PII (see the canary audit), so this view is never written to disk or the report.")
            st.dataframe(pd.DataFrame(st.session_state["last_masked_rows"]), hide_index=True, width="stretch")

    if batch_record:
        st.subheader(f"Latency: `{batch_record['run_id']}`")
        gw = batch_record.get("gateway") or {}
        st.caption(
            f"Backend **{gw.get('model_backend')}** · {gw.get('llm_model')} · {batch_record['input']['questions']} questions · "
            f"status {batch_record['status_counts']} · percentiles computed by the gateway from stored samples"
        )
        stages = (batch_record.get("latency") or {}).get("stages") or {}
        if stages:
            df = pd.DataFrame(
                [{"stage": s, **{k: stages[s][k] for k in ("n", "avg", "p50", "p90", "p95", "p99")}, "what it times": STAGE_NOTES[s]}
                 for s in STAGES if s in stages]
            )
            st.dataframe(df, hide_index=True, width="stretch",
                         column_config={k: st.column_config.NumberColumn(format="%.0f") for k in ("avg", "p50", "p90", "p95", "p99")})
            for note in combine_notes(batch_record["latency"].get("notes", [])):
                st.caption(f"Note: {note}")
            st.altair_chart(latency_chart([p for r in batch_record["requests"] for p in timing_points(r)]), width="stretch")
        else:
            st.warning("No latency samples for this run (no request succeeded).")


# --- Tab 2: saved results -----------------------------------------------------

def source_caption(summary: dict, extra: str = "") -> None:
    src = summary["source"]
    st.caption(f"`reports/{src['file']}` · started {src['started_at']} · commit `{src['git_commit']}`{extra}")


with tab_results:
    sub_rag, sub_det, sub_can = st.tabs(["RAG eval", "Detector accuracy", "Canary audit"])
    with sub_rag:
        if not rag_s:
            st.info("No RAG eval report. Run `scripts/eval_rag.py` on the host.")
        else:
            source_caption(rag_s, f" · backend **{rag_s['model_backend'] or 'not recorded'}** · {rag_s['llm_model']}")
            c = st.columns(4)
            c[0].metric("Retrieved in top k", f"{rag_s['retrieval_hits']}/{rag_s['answerable']}")
            c[1].metric("Answers correct", f"{rag_s['answers_correct']}/{rag_s['answerable']}" if rag_s["answers_correct"] is not None else "—")
            c[2].metric("Refusals correct", f"{rag_s['refusals_correct']}/{rag_s['refusal_cases']}" if rag_s["refusals_correct"] is not None else "—")
            c[3].metric("Evidence cited", f"{rag_s['citations_hit']}/{rag_s['answerable']}" if rag_s["citations_hit"] is not None else "—")
            st.dataframe(pd.DataFrame(rag_s["rows"]), hide_index=True, width="stretch")
    with sub_det:
        if not det_s:
            st.info("No detector report. Run `scripts/eval_detector.py` on the host.")
        else:
            source_caption(det_s, f" · `{det_s['scrubber']}`")
            o = det_s["overall"]
            c = st.columns(3)
            c[0].metric("Recall (overall)", f"{o['recall']:.1%}")
            c[1].metric("Precision (overall)", f"{o['precision']:.1%}")
            neg = det_s["negatives"] or {}
            c[2].metric("No-PII records flagged", f"{neg.get('flagged')}/{neg.get('total')}")
            df = pd.DataFrame([{"entity": k, **{f: v[f] for f in ("support", "precision", "recall", "tp", "fp", "fn")}}
                               for k, v in det_s["per_entity"].items()])
            st.dataframe(df, hide_index=True, width="stretch",
                         column_config={f: st.column_config.NumberColumn(format="percent") for f in ("precision", "recall")})
    with sub_can:
        if not can_s:
            st.info("No canary report. Run `scripts/canary_audit.py` on the host.")
        else:
            source_caption(can_s, f" · run `{can_s['run_id']}` · backend **{reports.backend_of(can_s) or 'not recorded'}**")
            planted = can_s["planted"]["total"]
            c = st.columns(2)
            c[0].metric("Unmasked by the Trust Engine", f"{can_s['unmasked']['total']}/{planted}",
                        help="In the masked question: the embedding model and LLM received the value.")
            c[1].metric("Found in answers, logs, or storage", f"{can_s['downstream']['total']}/{planted}")
            df = pd.DataFrame([{"entity": e, "planted": n, "unmasked": can_s["unmasked"]["by_entity"].get(e, 0),
                                "in answers, logs, or storage": can_s["downstream"]["by_entity"].get(e, 0)}
                               for e, n in sorted(can_s["planted"]["by_entity"].items())])
            st.dataframe(df, hide_index=True, width="stretch")
            st.write("Found by location:", can_s["found"].get("by_location") or "nowhere")
            if can_s["not_searched"] is None:
                st.warning("This report predates the not-searched check; log coverage was not verified.")
            for gap in can_s["not_searched"] or []:
                st.warning(f"Not searched: {gap}")


# --- Tab 3: report ------------------------------------------------------------

with tab_report:
    st.subheader("Download the report")
    st.caption("One self-contained HTML file (no external links; opens offline). Sources are the files selected in the sidebar.")
    for label, path in (("Batch run", batch_path), ("RAG eval", rag_path), ("Detector eval", det_path), ("Canary audit", can_path)):
        st.write(f"- **{label}:** " + (f"`{path.name}`" if path else "_none_"))
    html = build_report(batch_record, rag_s, det_s, can_s, datetime.now(timezone.utc))
    name = f"sandbox_report_{(batch_record or {}).get('run_id', 'no-batch')}.html"
    st.download_button("Download HTML report", html, file_name=name, mime="text/html", type="primary")
    with st.expander("Preview"):
        # Our own escaped HTML (no scripts); never user-supplied markup.
        st.iframe(html, height=900)
