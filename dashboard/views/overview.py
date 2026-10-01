"""Overview: one verdict per buyer question, each headline number against its target."""

import streamlit as st

import ui
from context import Context, runs_shown
from reports import backend_label, backend_of


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
                + "). Each card names the run or configuration it comes from.", icon=":material/info:")

    cols = st.columns(2)
    for i, card in enumerate(ctx.cards):
        with cols[i % 2].container(border=True, height="stretch"):
            st.caption(card.title.upper())
            ui.status_badge(card.status)
            # Short numbers read as a figure; a sentence headline (isolation) reads as a statement.
            st.markdown(f"## {card.headline}" if len(card.headline) <= 12 else f"##### {card.headline}")
            if card.subhead:
                st.caption(card.subhead)
            for line in card.lines:
                st.caption(line)
            st.write(card.sentence)
            page = ctx.pages.get(card.question)
            if page is not None:
                st.page_link(page, label="Details", icon=":material/arrow_forward:")

    with ui.evidence("acceptance criteria and runs"):
        st.caption("Targets come from `dashboard/acceptance.toml`. A miss counts as a known gap only when the file "
                   "names its documented cause; otherwise it's a fail.")
        ui.criteria_table(ctx.results)
        for line in shown:
            st.markdown(f"- {line}")
