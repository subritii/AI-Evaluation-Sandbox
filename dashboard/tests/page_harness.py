"""Script run by AppTest: renders one page against fixture reports, with no gateway.

Environment: HARNESS_REPORTS_DIR, HARNESS_PAGE, optional HARNESS_TRACE (an eval question id).
"""

import os
from pathlib import Path

import streamlit as st

from context import default_paths, load_context
from views import accuracy, isolation, overview, performance, privacy, report, run_test

reports_dir = Path(os.environ["HARNESS_REPORTS_DIR"])
ctx = load_context(reports_dir, "http://127.0.0.1:9", None, default_paths(reports_dir))
if os.environ.get("HARNESS_TRACE"):
    st.session_state["accuracy_trace_id"] = os.environ["HARNESS_TRACE"]
pages = {"overview": overview, "accuracy": accuracy, "privacy": privacy, "isolation": isolation,
         "performance": performance, "run_test": run_test, "report": report}
pages[os.environ["HARNESS_PAGE"]].render(ctx)
