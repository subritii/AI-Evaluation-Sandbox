"""Accuracy: does it answer policy questions correctly, and can every answer be traced?"""

import pandas as pd
import streamlit as st

import ui
from batch import STAGES
from context import Context
from glossary import term
from reports import backend_label, compare_evals


def _pf(value) -> str:
    return "—" if value is None else ("✅ pass" if value else "❌ fail")


def render_trace(case: dict, run_note: str) -> None:
    """Question → retrieved chunks → answer → citations → timings, for one eval question."""
    with st.container(border=True):
        st.markdown(f"#### Trace: `{case['id']}`")
        st.caption(run_note)
        st.markdown(f"**Question** ({'should be refused' if case['type'] == 'refuse' else 'answerable'})")
        st.info(case["question"], icon=":material/help:")

        st.markdown("**Retrieved chunks**")
        cited = {c["ref"] for c in case["citations"]}
        if case["retrieved"]:
            df = pd.DataFrame([
                {"rank": i + 1, "chunk": r["ref"], "section": r.get("heading", "").lstrip("# "),
                 "similarity": r["similarity"], "cited": "yes" if r["ref"] in cited else "",
                 "excerpt": r.get("excerpt") or "(not stored in this report)"}
                for i, r in enumerate(case["retrieved"])
            ])
            st.dataframe(df, hide_index=True, width="stretch", column_config={
                "similarity": st.column_config.NumberColumn(format="%.3f", help=term("similarity")),
                "excerpt": st.column_config.TextColumn(width="large"),
            })
            if case["expected_rank"]:
                st.caption(f"The chunk holding the expected evidence was retrieved at rank {case['expected_rank']}.")

        st.markdown(f"**Answer** · {_pf(case['answer_pass'])}")
        if case["answer"] is None:
            st.caption("Retrieval-only run: no answer generated.")
        else:
            st.markdown(f"> {case['answer']}")
            if case["missing_keywords"]:
                st.caption("Missing expected content: " + "; ".join(" or ".join(g) for g in case["missing_keywords"]))
            if case["refused"] and case["type"] == "answer":
                st.caption("The answer refused or hedged on a question the policy covers, which counts as a failure.")

        st.markdown(f"**Citations** (attached by code, not written by the model) · {_pf(case['citation_pass'])}")
        if case["citations"]:
            st.dataframe(pd.DataFrame([
                {"chunk": c["ref"], "section": c.get("section"), "support": c.get("support"),
                 "holds the evidence": "yes" if c.get("has_evidence") else "no"}
                for c in case["citations"]
            ]), hide_index=True, width="stretch",
                column_config={"support": st.column_config.NumberColumn(format="%.2f", help=term("support"))})
        else:
            st.caption("No citations" + (" (correct for a refusal)." if case["type"] == "refuse" else "."))

        st.markdown("**Per-stage timings**")
        t = case["timings_ms"]
        cols = st.columns(len([s for s in STAGES if s in t]) or 1)
        for col, stage in zip(cols, [s for s in STAGES if s in t]):
            col.metric(stage.replace("_", " "), f"{t[stage]:,.0f} ms")


def render(ctx: Context) -> None:
    st.title("Accuracy")
    st.caption("Does it answer policy questions correctly, refuse what the policy doesn't cover, and cite the right section?")
    ui.criteria_table(ctx.results_for("accuracy"))

    rag = ctx.rag
    if not rag:
        st.info("No RAG eval report. Run `docker compose run --rm tools python scripts/eval_rag.py`.")
        return
    run_note = (f"{rag['source']['file']} · {backend_label(rag['model_backend'])} · {rag['llm_model']} · "
                f"commit {rag['source']['git_commit']}")

    c = st.columns(4)
    c[0].metric("Retrieved in top k", f"{rag['retrieval_hits']}/{rag['answerable']}",
                help="The chunk holding the answer was among the chunks retrieved for the question.")
    c[1].metric("Answers correct", f"{rag['answers_correct']}/{rag['answerable']}" if rag["answers_correct"] is not None else "—",
                help="Contains the expected facts and doesn't hedge or refuse.")
    c[2].metric("Refusals correct", f"{rag['refusals_correct']}/{rag['refusal_cases']}" if rag["refusals_correct"] is not None else "—",
                help="Questions the policy doesn't cover, correctly declined.")
    c[3].metric("Evidence cited", f"{rag['citations_hit']}/{rag['answerable']}" if rag["citations_hit"] is not None else "—",
                help="A code-attached citation points at the chunk that holds the answer.")

    st.subheader("Questions")
    st.caption("Select a row to open its trace. These are the eval's own synthetic questions, so they can be shown.")
    ids = list(rag["cases"])
    table = pd.DataFrame([
        {"question": rag["cases"][i]["question"], "type": r["type"],
         "retrieval": _pf(r["retrieval"]), "answer": _pf(r["answer"]), "citation": _pf(r["citation"]),
         "total ms": r["total_ms"]}
        for i, r in zip(ids, rag["rows"])
    ])
    event = st.dataframe(table, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                         key="accuracy_questions",
                         column_config={"total ms": st.column_config.NumberColumn(format="%.0f"),
                                        "question": st.column_config.TextColumn(width="large")})
    rows = getattr(getattr(event, "selection", None), "rows", None) or []
    picked = ids[rows[0]] if rows else st.session_state.get("accuracy_trace_id")
    if picked in rag["cases"]:
        render_trace(rag["cases"][picked], run_note)

    st.subheader("Before and after")
    if ctx.baseline:
        cmp = compare_evals(ctx.baseline, rag)
        (b_ok, b_n), (a_ok, a_n) = cmp["baseline"]["answers"], cmp["current"]["answers"]
        c = st.columns(2)
        c[0].metric("Baseline: answers correct", f"{b_ok}/{b_n}", help=ctx.config.baseline_label)
        c[1].metric("Current: answers correct", f"{a_ok}/{a_n}", delta=(a_ok - b_ok) if a_n == b_n else None)
        st.caption(f"Baseline: {ctx.config.baseline_label}, `{ctx.baseline['source']['file']}`, commit "
                   f"`{ctx.baseline['source']['git_commit']}`, backend "
                   f"{backend_label(ctx.baseline['model_backend'])}. Current: `{rag['source']['file']}`, "
                   f"{backend_label(rag['model_backend'])}. Several things changed between them (prompt, citations, "
                   "and possibly the model runtime), so the gain isn't attributable to one change.")
        st.dataframe(pd.DataFrame([
            {"question": r["id"], "type": r["type"], "baseline": _pf(r["baseline"]), "current": _pf(r["current"]),
             "change": r["change"]}
            for r in cmp["rows"]
        ]), hide_index=True, width="stretch")
    else:
        st.caption(f"Baseline report `{ctx.config.baseline_report}` not found in reports/.")

    with ui.evidence("RAG eval run"):
        ui.source_caption(rag, f" · backend **{rag['model_backend'] or 'not recorded'}** · {rag['llm_model']}")
        st.dataframe(pd.DataFrame(rag["rows"]), hide_index=True, width="stretch")
