"""Run a test: upload a question batch, watch per-stage latency live, trace each answer (session only)."""

from dataclasses import asdict

import altair as alt
import pandas as pd
import streamlit as st

import ui
from batch import MAX_ROWS, STAGES, BatchError, RequestResult, execute, file_sha256, parse_batch
from context import Context
from glossary import term
from report_html import combine_notes


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


def _live_row(r: dict) -> dict:
    return {
        "#": r["index"] + 1,
        "status": r["status"],
        "masked question (as the models saw it)": r["masked_question"] or r["error"],
        "entities masked": ", ".join(f"{k} {v}" for k, v in r["masked_entities"].items()),
        "total ms": round(r["timings_ms"].get("total", 0)),
    }


def render_live_trace(r: dict) -> None:
    """Trace for a live request, from session memory only."""
    with st.container(border=True):
        st.markdown(f"#### Trace: request {r['index'] + 1}")
        st.caption("Session only: this trace is never saved or put in the report, because masking can miss PII.")
        st.markdown("**Question after masking** (what the embedding model and LLM received)")
        st.info(r["masked_question"] or f"No response: {r['error']}", icon=":material/shield:")
        if r["masked_entities"]:
            st.caption("Masked: " + ", ".join(f"{k} ×{v}" for k, v in r["masked_entities"].items()))
        if r["answer"] is not None:
            st.markdown("**Answer**" + (" (refused)" if r["refused"] else ""))
            st.markdown(f"> {r['answer']}")
        st.markdown("**Citations** (attached by code)")
        if r["citation_detail"]:
            st.dataframe(pd.DataFrame([
                {"chunk": f"{c.get('source')}#{c.get('chunk_index')}", "section": c.get("section"),
                 "similarity": c.get("similarity"), "support": c.get("support")}
                for c in r["citation_detail"]
            ]), hide_index=True, width="stretch", column_config={
                "similarity": st.column_config.NumberColumn(format="%.3f", help=term("similarity")),
                "support": st.column_config.NumberColumn(format="%.2f", help=term("support")),
            })
        else:
            st.caption("None.")
        t = r["timings_ms"]
        present = [s for s in STAGES if s in t]
        for col, stage in zip(st.columns(len(present) or 1), present):
            col.metric(stage.replace("_", " "), f"{t[stage]:,.0f} ms")


def render(ctx: Context) -> None:
    st.title("Run a test")
    st.caption(
        f"Upload a CSV with a `question` column, or JSONL with one `{{\"question\": ...}}` per line (up to {MAX_ROWS}). "
        "The file stays in memory: it is never saved, logged, or shown. Saved runs keep counts and timings only."
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

    run = st.button("Run batch", type="primary", disabled=not (questions and ctx.info))
    if not ctx.info:
        st.caption(f"Gateway not reachable at {ctx.api}.")
    if run and questions:
        progress = st.progress(0.0, text="Warming up…")
        chart_slot, table_slot = st.empty(), st.empty()
        points: list[dict] = []
        live: list[dict] = []

        def on_result(r: RequestResult) -> None:
            row = asdict(r)
            live.append(row)
            points.extend(timing_points(row))
            progress.progress((r.index + 1) / len(questions), text=f"{r.index + 1}/{len(questions)} requests")
            if points:
                chart_slot.altair_chart(latency_chart(points), width="stretch")
            table_slot.dataframe(pd.DataFrame([_live_row(x) for x in live[-15:]]), hide_index=True, width="stretch")

        try:
            record, path = execute(upload.name, upload.getvalue(), ctx.api, int(warmup), ctx.reports_dir, on_result=on_result)
        except (BatchError, ConnectionError) as exc:
            st.error(str(exc))
        else:
            st.session_state["last_batch_path"] = path
            st.session_state["select_dashboard_batch"] = True
            st.session_state["live_results"] = live  # session memory only
            st.rerun()  # reload so the new run is the selected batch everywhere

    batch = ctx.batch
    live = st.session_state.get("live_results")
    if live and ctx.paths.get("dashboard_batch") == st.session_state.get("last_batch_path"):
        st.subheader("Last run: requests (this session only)")
        st.caption("Select a row to trace it. Masked questions and answers are kept in this browser session only.")
        event = st.dataframe(pd.DataFrame([_live_row(x) for x in live]), hide_index=True, width="stretch",
                             on_select="rerun", selection_mode="single-row", key="live_requests")
        rows = getattr(getattr(event, "selection", None), "rows", None) or []
        if rows:
            render_live_trace(live[rows[0]])

    if batch:
        gw = batch.get("gateway") or {}
        st.subheader(f"Latency: `{batch['run_id']}`")
        st.caption(f"Backend **{gw.get('model_backend')}** · {gw.get('llm_model')} · {batch['input']['questions']} "
                   f"questions · status {batch['status_counts']} · percentiles computed by the gateway")
        stages = (batch.get("latency") or {}).get("stages") or {}
        if stages:
            ui.latency_frame(stages)
            for note in combine_notes(batch["latency"].get("notes", [])):
                st.caption(f"Note: {note}")
            st.altair_chart(latency_chart([p for r in batch["requests"] for p in timing_points(r)]), width="stretch")
        else:
            st.warning("No latency samples for this run (no request succeeded).")
        with ui.evidence("batch run"):
            st.caption(f"`reports/{ctx.paths['dashboard_batch'].name}` · input `{batch['input']['file_name']}` · "
                       f"sha256 `{batch['input']['sha256'][:16]}…` · masked entities {batch['masked_entity_totals']}")
            st.dataframe(pd.DataFrame([
                {"#": r["index"] + 1, "status": r["status"], "entities": r["masked_entities"], "refused": r["refused"],
                 "citations": r["citations"], **{s: r["timings_ms"].get(s) for s in STAGES}}
                for r in batch["requests"]
            ]), hide_index=True, width="stretch")
