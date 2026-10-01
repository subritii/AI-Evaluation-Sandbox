"""Performance: how much time does the PII scan add, and how long does a full answer take, per configuration?"""

import altair as alt
import pandas as pd
import streamlit as st

import criteria
import ui
from batch import STAGES
from context import Context, run_date
from glossary import term
from reports import backend_label


def _status_for(ctx: Context, conf_id: str) -> str:
    return criteria.worst([r.status for r in ctx.results if r.configuration and r.configuration.id == conf_id])


def render(ctx: Context) -> None:
    st.title("Performance")
    st.caption("How much time does the PII scan add, and how long does a full answer take? Each deployment "
               "configuration is measured on its own largest saved run.")
    ui.criteria_table(ctx.results_for("performance"))

    confs = list(ctx.config.configurations.values())
    runs = {c.id: criteria.configuration_run(ctx.latency_runs, c) for c in confs}
    if not any(runs.values()):
        st.info("No saved latency runs. Run a batch on the Run a test page, or `scripts/canary_audit.py`.")
        return

    st.subheader("By configuration")
    rows = []
    for conf in confs:
        run = runs[conf.id]
        if not run:
            rows.append({"configuration": conf.label, "status": ui.status_text(criteria.NO_DATA)})
            continue
        s = run["latency"]["stages"]
        rows.append({
            "configuration": conf.label, "status": ui.status_text(_status_for(ctx, conf.id)),
            "PII scan P50": s["pii_scan"]["p50"], "PII scan P95": s["pii_scan"]["p95"],
            "end-to-end P50": s["total"]["p50"], "end-to-end P95": s["total"]["p95"],
            "n": run["n"], "run": run["run_id"], "date": run_date(run["started_at"]),
            "load (1m) at start": round(run["load_at_start"], 1) if run["load_at_start"] is not None else None,
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
        **{k: st.column_config.NumberColumn(format="%.0f ms", help=term("p95") if "P95" in k else term("p50"))
           for k in ("PII scan P50", "PII scan P95", "end-to-end P50", "end-to-end P95")},
        "load (1m) at start": st.column_config.NumberColumn(
            help="1-minute load average when the run started. Inference on the CPU loads the whole machine."),
    })
    for r in ctx.results_for("performance"):
        if r.note and r.configuration:
            st.caption(f"⚠️ {r.configuration.label}: {r.note}")
            break

    chart_rows = [
        {"stage": stage, "configuration": conf.label, "P95 ms": runs[conf.id]["latency"]["stages"][stage]["p95"]}
        for conf in confs if runs[conf.id] for stage in STAGES if stage in runs[conf.id]["latency"]["stages"]
    ]
    # A dot plot, not bars: the configurations differ ~300x, which needs a log
    # scale, and bars on a log scale have no zero to start from (they vanish).
    st.altair_chart(
        alt.Chart(pd.DataFrame(chart_rows)).mark_circle(size=140, opacity=0.9).encode(
            y=alt.Y("stage:N", sort=list(STAGES), title=None),
            x=alt.X("P95 ms:Q", title="P95, milliseconds (log scale)", scale=alt.Scale(type="log")),
            color=alt.Color("configuration:N", title=None, legend=alt.Legend(orient="bottom")),
            tooltip=["configuration", "stage", alt.Tooltip("P95 ms:Q", format=",.0f")],
        ).properties(height=260),
        width="stretch",
    )

    tabs = st.tabs([c.label for c in confs if runs[c.id]])
    for tab, conf in zip(tabs, [c for c in confs if runs[c.id]]):
        with tab:
            run = runs[conf.id]
            st.caption(f"{run['run_id']} · {run['n']} requests · {', '.join(backend_label(b) for b in run['model_backends'])} "
                       f"· providers {', '.join(run['providers'])} · {run_date(run['started_at'])}")
            ui.latency_frame(run["latency"]["stages"])
            for note in run["latency"].get("notes", []):
                st.caption(f"Note: {note}")

    st.subheader("All latency runs")
    st.caption("Every saved run with latency, so nothing is hidden. Runs on different backends aren't comparable: "
               "native Ollama uses the Apple GPU, the Docker container runs on the CPU.")
    st.dataframe(pd.DataFrame([
        {"run": r["file"], "kind": "canary audit" if r["kind"] == "canary_audit" else "dashboard batch",
         "date": run_date(r["started_at"]), "n": r["n"], "backend": ", ".join(map(str, r["model_backends"])),
         "providers": ", ".join(r["providers"]),
         "configuration": next((c.label for c in confs if criteria.run_matches(r, c)), "—"),
         "PII scan P95 ms": r["latency"]["stages"].get("pii_scan", {}).get("p95"),
         "total P95 ms": r["latency"]["stages"]["total"]["p95"],
         "load (1m) at start": round(r["load_at_start"], 1) if r["load_at_start"] is not None else None}
        for r in ctx.latency_runs
    ]), hide_index=True, width="stretch", column_config={
        "PII scan P95 ms": st.column_config.NumberColumn(format="%.0f", help=term("p95")),
        "total P95 ms": st.column_config.NumberColumn(format="%.0f", help=term("p95")),
    })

    with ui.evidence("latency method"):
        st.markdown(
            "- Timed inside the gateway with `time.perf_counter()`, per stage, stored per request in Postgres "
            "with the model backend and provider on every row.\n"
            "- Requests are sent one at a time; warmup requests are excluded.\n"
            "- Percentiles use numpy's linear interpolation over all samples of the run.\n"
            "- A configuration's row uses its largest saved run (newest on a tie). A run belongs to a configuration "
            "when its backend matches and both models used the configuration's providers (`acceptance.toml`)."
        )
