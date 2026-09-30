"""Privacy: does PII get masked before the models, and does any reach answers, logs, or storage?"""

import pandas as pd
import streamlit as st

import ui
from context import Context
from glossary import term
from reports import backend_label, backend_of


def render(ctx: Context) -> None:
    st.title("Privacy")
    st.caption("Is PII masked before the models see it, and does any planted PII reach answers, logs, or storage?")
    ui.criteria_table(ctx.results_for("privacy"))

    can, det = ctx.can, ctx.det
    st.subheader("Canary audit")
    if not can:
        st.info("No canary audit report. Run `.venv/bin/python scripts/canary_audit.py`.")
    else:
        planted = can["planted"]["total"]
        c = st.columns(3)
        c[0].metric("Canaries planted", planted, help=term("canary"))
        c[1].metric("Found in answers, logs, or storage", f"{can['downstream']['total']}/{planted}", help=term("downstream"))
        c[2].metric("Unmasked by the Trust Engine", f"{can['unmasked']['total']}/{planted}", help=term("unmasked"))
        st.caption(f"{can['requests']} requests, {backend_label(backend_of(can))}. All planted values are synthetic.")
        st.dataframe(pd.DataFrame([
            {"entity": e, "planted": n, "unmasked": can["unmasked"]["by_entity"].get(e, 0),
             "in answers, logs, or storage": can["downstream"]["by_entity"].get(e, 0)}
            for e, n in sorted(can["planted"]["by_entity"].items())
        ]), hide_index=True, width="stretch", column_config={
            "unmasked": st.column_config.NumberColumn(help=term("unmasked")),
            "in answers, logs, or storage": st.column_config.NumberColumn(help=term("downstream")),
        })
        if can["not_searched"] is None:
            st.warning("This report predates the not-searched check; log coverage was not verified.")
        for gap in can["not_searched"] or []:
            st.warning(f"Not searched: {gap}")

    st.subheader("PII detector accuracy")
    if not det:
        st.info("No detector report. Run `.venv/bin/python scripts/eval_detector.py`.")
    else:
        o = det["overall"]
        c = st.columns(3)
        c[0].metric("Recall (overall)", f"{o['recall']:.1%}", help=term("recall"))
        c[1].metric("Precision (overall)", f"{o['precision']:.1%}", help=term("precision"))
        neg = det["negatives"] or {}
        c[2].metric("No-PII records flagged", f"{neg.get('flagged')}/{neg.get('total')}",
                    help="Records with no PII where the detector flagged something anyway (over-masking).")
        st.dataframe(pd.DataFrame([
            {"entity": k, **{f: v[f] for f in ("support", "precision", "recall", "tp", "fp", "fn")}}
            for k, v in det["per_entity"].items()
        ]), hide_index=True, width="stretch", column_config={
            "precision": st.column_config.NumberColumn(format="percent", help=term("precision")),
            "recall": st.column_config.NumberColumn(format="percent", help=term("recall")),
            "support": st.column_config.NumberColumn(help="Labeled spans of this type in the dataset."),
        })
        split = det["by_difficulty"]
        if split:
            st.caption(" · ".join(
                f"{label}: recall {split[k]['recall']:.1%} ({split[k]['support']} spans)"
                for k, label in (("standard", "Standard templates"), ("hard", "Hard templates")) if k in split
            ))

    with ui.evidence("canary audit run"):
        if can:
            ui.source_caption(can, f" · run `{can['run_id']}`")
            st.write("Found by location:", can["found"].get("by_location") or "nowhere")
            st.write("Searched:", can.get("searched") or "not recorded")
    with ui.evidence("detector eval run"):
        if det:
            ui.source_caption(det, f" · `{det['scrubber']}` · threshold {det['threshold']}")
            st.write("Dataset:", det["dataset"])
