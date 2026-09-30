"""Performance: how much time does the PII scan add, and how long does a full answer take?"""

import altair as alt
import pandas as pd
import streamlit as st

import reports
import ui
from batch import STAGES
from context import Context, run_date
from glossary import term


def all_latency_runs(reports_dir) -> list[dict]:
    """Every saved run with latency: canary audits and dashboard batches, newest first."""
    rows = []
    for kind in ("canary_audit", "dashboard_batch"):
        for path in reports.list_reports(reports_dir, kind):
            try:
                raw = reports.load(path)
            except (OSError, ValueError):
                continue
            latency = raw.get("latency") or {}
            stages = latency.get("stages") or {}
            if "total" not in stages:
                continue
            models = latency.get("models") or []
            backends = {m["model_backend"] for m in models} or {(raw.get("gateway") or {}).get("model_backend")}
            env = raw.get("environment") or {}
            load = env.get("host_load_avg_before") or env.get("gateway_load_avg_before")
            rows.append({
                "run": path.name, "kind": "canary audit" if kind == "canary_audit" else "dashboard batch",
                "date": run_date(raw.get("started_at")), "n": stages["total"]["n"],
                "backend": ", ".join(sorted(str(b) for b in backends)),
                "PII scan P95 ms": stages.get("pii_scan", {}).get("p95"), "total P95 ms": stages["total"]["p95"],
                "load (1m) at start": round(load[0], 1) if load else None,
            })
    return rows


def render(ctx: Context) -> None:
    st.title("Performance")
    st.caption("How much time does the PII scan add, and how long does a full answer take?")
    ui.criteria_table(ctx.results_for("performance"))

    headline = reports.headline_latency(ctx.batch, ctx.can)
    if not headline:
        st.info("No latency run selected. Run a batch on the Run a test page, or `scripts/canary_audit.py`.")
        return
    latency, label = headline
    stages = latency["stages"]
    st.caption(f"Headline run: {label}. The run with the most samples is used, so P95 rests on as many requests as possible.")

    c = st.columns(3)
    c[0].metric("PII scan P95", f"{stages['pii_scan']['p95']:,.0f} ms", help=term("p95"))
    c[1].metric("End-to-end P95", f"{stages['total']['p95']:,.0f} ms", help=term("p95"))
    c[2].metric("PII scan share of end-to-end (P95)", f"{stages['pii_scan']['p95'] / stages['total']['p95']:.1%}",
                help="How much of a slow request's time the privacy layer accounts for.")

    chart_df = pd.DataFrame([
        {"stage": s, "percentile": p.upper(), "ms": stages[s][p]} for s in STAGES if s in stages for p in ("p50", "p95")
    ])
    st.altair_chart(
        alt.Chart(chart_df).mark_bar().encode(
            y=alt.Y("stage:N", sort=list(STAGES), title=None),
            x=alt.X("ms:Q", title="milliseconds"),
            yOffset=alt.YOffset("percentile:N"),
            color=alt.Color("percentile:N", title=None),
            tooltip=["stage", "percentile", alt.Tooltip("ms:Q", format=",.0f")],
        ).properties(height=260),
        width="stretch",
    )
    ui.latency_frame(stages)
    for note in latency.get("notes", []):
        st.caption(f"Note: {note}")

    st.subheader("All latency runs")
    st.caption("Every saved run with latency, so the headline can be compared with the others. Runs on different "
               "backends aren't comparable: native Ollama uses the Apple GPU, the Docker container runs on CPU.")
    runs = all_latency_runs(ctx.reports_dir)
    if runs:
        st.dataframe(pd.DataFrame(runs), hide_index=True, width="stretch", column_config={
            "PII scan P95 ms": st.column_config.NumberColumn(format="%.0f", help=term("p95")),
            "total P95 ms": st.column_config.NumberColumn(format="%.0f", help=term("p95")),
            "load (1m) at start": st.column_config.NumberColumn(
                help="1-minute load average when the run started (host for scripts, Docker VM for dashboard runs)."),
        })

    with ui.evidence("latency method"):
        st.markdown(
            "- Timed inside the gateway with `time.perf_counter()`, per stage, stored per request in Postgres "
            "with the model backend and provider on every row.\n"
            "- Requests are sent one at a time; warmup requests are excluded.\n"
            "- Percentiles use numpy's linear interpolation over all samples of the run."
        )
