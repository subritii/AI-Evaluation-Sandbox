"""Isolation: can the containers that handle data reach the internet?"""

import pandas as pd
import streamlit as st

import ui
from context import Context
from glossary import term
from reports import backend_label


def _probe_rows(label: str, probe: dict) -> list[dict]:
    return [{"from": label, "target": target, "result": result} for target, result in probe.items()]


def render(ctx: Context) -> None:
    st.title("Isolation")
    st.caption("Is the sandbox cut off from the internet while it runs?", help=term("air_gap"))
    ui.criteria_table(ctx.results_for("isolation"))

    air = ctx.airgap
    if not air:
        st.info("No air-gap check report. Run `.venv/bin/python scripts/airgap_check.py` with the stack up.")
        return

    c = st.columns(4)
    c[0].metric("Verdict", air["verdict"], help=term("air_gap"))
    c[1].metric("Isolated containers", len(air["isolated"]), help=", ".join(air["isolated"]))
    c[2].metric("Control probe connected", "yes" if air["control_connected"] else "no",
                help="The same probe on a normal Docker network must connect; otherwise 'blocked' could just mean "
                     "the machine was offline.")
    c[3].metric("Real query answered", "yes" if air["functional_status"] == 200 else
                ("not run" if air["functional_status"] is None else f"HTTP {air['functional_status']}"))
    st.caption(f"Configuration: {backend_label(air['model_backend'])}. "
               + ("Isolation is enforced in the all-Docker configuration only; native and remote endpoints are "
                  "outside it." if air["model_backend"] != "docker" else ""))
    for problem in air["problems"]:
        st.error(problem, icon=":material/error:")
    for note in air["notes"]:
        st.caption(note)

    st.subheader("Containers")
    st.dataframe(pd.DataFrame([
        {"service": c_["service"], "route to the internet": "no" if c_["isolated"] else "yes",
         "networks": ", ".join(f"{n.split('_', 1)[-1]} ({kind})" for n, kind in c_["networks"].items())
         if isinstance(c_.get("networks"), dict) else "",
         "published ports": ", ".join(c_.get("published_ports", [])) or "none"}
        for c_ in air["inventory"]
    ]), hide_index=True, width="stretch")

    with ui.evidence("air-gap probes"):
        ui.source_caption(air, f" · verdict {air['verdict']}")
        probes = air["probes"]
        rows = []
        for service, result in probes["in_container"].items():
            rows += _probe_rows(f"inside {service}", result)
        rows += _probe_rows("sandbox network", probes["sandbox_network"])
        rows += _probe_rows("control (normal network)", probes["control"])
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.write("Internal reachability:", probes["internal"])
        st.write("Ingress through the proxy:", probes["ingress"])
