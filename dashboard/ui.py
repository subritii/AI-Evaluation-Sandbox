"""Shared Streamlit building blocks: status badges, criteria tables, evidence sections."""

import pandas as pd
import streamlit as st

import criteria
from batch import STAGES
from glossary import term
from report_html import STAGE_NOTES

BADGE_COLOR = {criteria.PASS: "green", criteria.KNOWN_GAP: "orange", criteria.FAIL: "red", criteria.NO_DATA: "gray"}


def status_badge(status: str) -> None:
    st.badge(f"{criteria.STATUS_ICON[status]} {criteria.STATUS_LABEL[status]}", color=BADGE_COLOR[status])


def status_text(status: str) -> str:
    return f"{criteria.STATUS_ICON[status]} {criteria.STATUS_LABEL[status]}"


def criteria_table(results: list[criteria.Result]) -> None:
    """Every headline number against its target, with status and (for known gaps) the documented cause."""
    if not results:
        return
    df = pd.DataFrame([
        {"Status": status_text(r.status), "Criterion": r.criterion.label, "Result": r.value_text,
         "Target": r.target_text, "Measured on": r.measurement.source if r.measurement else "no selected report",
         "Known gap": r.note}
        for r in results
    ])
    st.dataframe(df, hide_index=True, width="stretch", column_config={
        "Status": st.column_config.TextColumn(help="Pass: meets the target. Known gap: misses it for a documented "
                                                   "reason. Fail: misses it with no documented reason. No data: no "
                                                   "selected report measures it."),
        "Target": st.column_config.TextColumn(help="From dashboard/acceptance.toml."),
        "Known gap": st.column_config.TextColumn(width="large"),
    })


def source_caption(summary: dict, extra: str = "") -> None:
    src = summary["source"]
    st.caption(f"`reports/{src['file']}` · started {src['started_at']} · commit `{src['git_commit']}`{extra}")


def evidence(title: str = "Evidence"):
    """Collapsed section for provenance and raw tables."""
    return st.expander(f"Evidence: {title}", expanded=False, icon=":material/description:")


def latency_frame(stages: dict) -> None:
    df = pd.DataFrame([
        {"stage": s, **{k: stages[s][k] for k in ("n", "avg", "p50", "p90", "p95", "p99")}, "what it times": STAGE_NOTES[s]}
        for s in STAGES if s in stages
    ])
    ms = {k: st.column_config.NumberColumn(format="%.0f") for k in ("avg", "p90")}
    ms |= {"p50": st.column_config.NumberColumn(format="%.0f", help=term("p50")),
           "p95": st.column_config.NumberColumn(format="%.0f", help=term("p95")),
           "p99": st.column_config.NumberColumn(format="%.0f", help=term("p99"))}
    st.dataframe(df, hide_index=True, width="stretch", column_config=ms)
