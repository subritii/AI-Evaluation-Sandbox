"""Overview: one verdict per buyer question, each headline number against its target."""

import streamlit as st

import criteria
import ui
from context import Context, runs_shown
from reports import backend_label, backend_of


def _result(ctx: Context, criterion_id: str) -> criteria.Result | None:
    return next((r for r in ctx.results if r.criterion.id == criterion_id), None)


def headline(ctx: Context, question: str) -> tuple[str, str]:
    """(headline number, plain-language sentence) for one question card."""
    if question == "accuracy":
        rag = ctx.rag
        if not rag or rag["answers_correct"] is None:
            return "—", "No RAG evaluation is selected."
        text = (f"Answered {rag['answers_correct']} of {rag['answerable']} policy questions correctly and refused "
                f"{rag['refusals_correct']} of {rag['refusal_cases']} questions the policy doesn't cover")
        if rag["citations_hit"] is not None:
            text += f", citing the right policy section for {rag['citations_hit']} of {rag['answerable']}"
        return f"{rag['answers_correct']}/{rag['answerable']}", text + "."
    if question == "privacy":
        can, det = ctx.can, ctx.det
        if not can:
            return "—", "No canary audit is selected."
        planted = can["planted"]["total"]
        text = (f"None of the {planted} planted fake PII values reached answers, logs, or storage."
                if can["downstream"]["total"] == 0 else
                f"{can['downstream']['total']} of {planted} planted fake PII values were found in answers, logs, or storage.")
        text += (f" The masking step missed {can['unmasked']['total']} before the models (bare numbers without "
                 "context, some names).")
        if det:
            text += f" Detector recall on labeled data: {det['overall']['recall']:.1%}."
        return f"{can['downstream']['total']}/{planted}", text
    if question == "isolation":
        air = ctx.airgap
        if not air:
            return "—", "No air-gap check is selected."
        if air["verdict"] == "PASS":
            return "PASS", ("Every container that handles data had no route to the internet, and a control probe on a "
                            "normal network confirmed the test can detect one. The ingress proxy is the only container "
                            f"with a route out ({backend_label(air['model_backend'])}).")
        first = air["problems"][0] if air["problems"] else "see Evidence"
        return air["verdict"], f"The selected check did not pass ({backend_label(air['model_backend'])}): {first}."
    if question == "performance":
        pii, total = _result(ctx, "pii_scan_p95"), _result(ctx, "end_to_end_p95")
        if not pii or not pii.measurement:
            return "—", "No latency run is selected."
        text = f"Masking PII adds {pii.value_text} at P95"
        if total and total.measurement:
            text += f"; a full answer takes {total.value_text} at P95"
        return pii.value_text, text + f" ({pii.measurement.source})."
    return "—", ""


def render(ctx: Context) -> None:
    st.title("Overview")
    if ctx.config.prepared_for:
        st.markdown(f"**Prepared for {ctx.config.prepared_for}** · proof-of-concept evaluation, synthetic data only")
    shown = runs_shown(ctx)
    st.caption("Runs shown: " + (" · ".join(shown) if shown else "no reports selected"))

    backends = {b for b in (ctx.rag and ctx.rag["model_backend"], ctx.can and backend_of(ctx.can),
                            ctx.airgap and ctx.airgap["model_backend"]) if b}
    if len(backends) > 1:
        st.info("These runs used different model backends (" + ", ".join(sorted(backend_label(b) for b in backends))
                + "). Each card names the run it comes from.", icon=":material/info:")

    cols = st.columns(2)
    for i, verdict in enumerate(ctx.verdicts):
        number, sentence = headline(ctx, verdict.question)
        with cols[i % 2].container(border=True, height="stretch"):
            st.caption(verdict.title.upper())
            ui.status_badge(verdict.status)
            st.markdown(f"## {number}")
            for r in verdict.results:
                st.caption(f"{criteria.STATUS_ICON[r.status]} {r.criterion.label}: {r.value_text} (target {r.target_text})")
            st.write(sentence)
            page = ctx.pages.get(verdict.question)
            if page is not None:
                st.page_link(page, label="Details", icon=":material/arrow_forward:")

    with ui.evidence("acceptance criteria and runs"):
        st.caption("Targets come from `dashboard/acceptance.toml`. A miss counts as a known gap only when the file "
                   "names its documented cause; otherwise it's a fail.")
        ui.criteria_table(ctx.results)
        for line in shown:
            st.markdown(f"- {line}")
