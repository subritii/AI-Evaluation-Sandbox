"""Report: one self-contained HTML file with every section, the acceptance criteria, and the methodology."""

from datetime import datetime, timezone

import streamlit as st

from context import KIND_LABELS, Context
from report_html import build_report


def html_for(ctx: Context) -> str:
    return build_report(ctx.batch, ctx.rag, ctx.det, ctx.can, datetime.now(timezone.utc), airgap=ctx.airgap,
                        criteria_results=ctx.results, prepared_for=ctx.config.prepared_for, cards=ctx.cards)


def render(ctx: Context) -> None:
    st.title("Report")
    st.caption("One self-contained HTML file: no external links, opens offline, contains no question text. "
               "It uses the runs chosen under Settings in the sidebar.")
    for kind, label in KIND_LABELS.items():
        path = ctx.paths.get(kind)
        st.write(f"- **{label}:** " + (f"`{path.name}`" if path else "_none_"))
    html = html_for(ctx)
    name = f"sandbox_report_{(ctx.batch or {}).get('run_id', 'no-batch')}.html"
    st.download_button("Download HTML report", html, file_name=name, mime="text/html", type="primary",
                       icon=":material/download:")
    with st.expander("Preview"):
        # Our own escaped HTML (no scripts); never user-supplied markup.
        st.iframe(html, height=900)
