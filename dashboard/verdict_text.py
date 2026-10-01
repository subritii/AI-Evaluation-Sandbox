"""Plain-language verdict cards, shared by the Overview page and the HTML report.

Pure functions over the criteria results and report summaries, so the
dashboard and the downloaded report always say the same thing.
"""

from dataclasses import dataclass

import criteria
from reports import backend_label


@dataclass(frozen=True)
class Card:
    question: str
    title: str
    status: str
    headline: str  # a number, or a short sentence when a number would mislead
    subhead: str  # where the headline comes from
    sentence: str
    lines: list[str]  # each criterion against its target


def _by_config(results: list[criteria.Result]) -> dict[str, list[criteria.Result]]:
    grouped: dict[str, list[criteria.Result]] = {}
    for r in results:
        grouped.setdefault(r.configuration.id if r.configuration else "", []).append(r)
    return grouped


def _accuracy(rag: dict | None) -> tuple[str, str, str]:
    if not rag or rag["answers_correct"] is None:
        return "—", "", "No RAG evaluation is selected."
    text = (f"Answered {rag['answers_correct']} of {rag['answerable']} policy questions correctly and refused "
            f"{rag['refusals_correct']} of {rag['refusal_cases']} questions the policy doesn't cover")
    if rag["citations_hit"] is not None:
        text += f", citing the right policy section for {rag['citations_hit']} of {rag['answerable']}"
    return (f"{rag['answers_correct']}/{rag['answerable']}", f"RAG eval, {backend_label(rag['model_backend'])}",
            text + ".")


def _privacy(can: dict | None, det: dict | None) -> tuple[str, str, str]:
    if not can:
        return "—", "", "No canary audit is selected."
    planted = can["planted"]["total"]
    text = (f"None of the {planted} planted fake PII values reached answers, logs, or storage."
            if can["downstream"]["total"] == 0 else
            f"{can['downstream']['total']} of {planted} planted fake PII values were found in answers, logs, or storage.")
    text += f" The masking step missed {can['unmasked']['total']} before the models (bare numbers without context, some names)."
    if det:
        text += f" Detector recall on labeled data: {det['overall']['recall']:.1%}."
    return f"{can['downstream']['total']}/{planted}", "planted PII found in answers, logs, or storage", text


def _isolation(air: dict | None) -> tuple[str, str, str]:
    if not air:
        return "No air-gap check selected", "", "Run scripts/airgap_check.py with the stack up."
    where = backend_label(air["model_backend"])
    if air["verdict"] == "PASS":
        return ("No container that handles data can reach the internet; only the ingress proxy can",
                f"air-gap check, {where}",
                "Connections out from inside the containers failed, while the same probe on a normal network "
                "connected (the control), and a real query was answered through the isolated stack. The proxy is "
                "the one exception because Docker can only publish ports from a network with a route out; it holds "
                "no data and runs a static, read-only config.")
    if air["verdict"] == "INCONCLUSIVE":
        return ("Isolation not established: the check couldn't confirm it can detect a route out",
                f"air-gap check, {where}",
                "The control probe on a normal network couldn't reach the internet either, so 'blocked' results "
                "can't be told apart from this machine being offline.")
    first = air["problems"][0] if air["problems"] else "see the Isolation page"
    return ("A container that handles data could reach the internet", f"air-gap check, {where}",
            f"The selected check failed: {first}.")


def _performance(results: list[criteria.Result], config: criteria.Config) -> tuple[str, str, str]:
    grouped = _by_config(results)
    head_conf = config.headline_configuration()
    head = grouped.get(head_conf.id if head_conf else "", [])
    pii = next((r for r in head if r.criterion.metric == "pii_scan_p95_ms" and r.measurement), None)
    if not pii:
        return "—", "", "No latency run for the headline configuration."
    parts = []
    for conf in config.configurations.values():
        rows = {r.criterion.metric: r for r in grouped.get(conf.id, []) if r.measurement}
        if "pii_scan_p95_ms" not in rows:
            parts.append(f"{conf.label}: no saved run.")
            continue
        n = rows["pii_scan_p95_ms"].measurement.run["n"]
        text = f"{conf.label} (n={n}): masking adds {rows['pii_scan_p95_ms'].value_text} at P95"
        if "total_p95_ms" in rows:
            text += f", a full answer takes {rows['total_p95_ms'].value_text}"
        gaps = {r.note for r in rows.values() if r.note}
        parts.append(text + (f"; a known gap: {' '.join(sorted(gaps))}" if gaps else "."))
    subhead = f"PII scan P95 · {head_conf.label}, {pii.measurement.source}"
    return pii.value_text, subhead, " ".join(parts)


def cards(results: list[criteria.Result], summaries: dict, config: criteria.Config) -> list[Card]:
    out = []
    for verdict in criteria.verdicts(results):
        if verdict.question == "accuracy":
            headline, subhead, sentence = _accuracy(summaries.get("rag"))
        elif verdict.question == "privacy":
            headline, subhead, sentence = _privacy(summaries.get("can"), summaries.get("det"))
        elif verdict.question == "isolation":
            headline, subhead, sentence = _isolation(summaries.get("airgap"))
        else:
            headline, subhead, sentence = _performance(verdict.results, config)
        lines = [f"{criteria.STATUS_ICON[r.status]} {r.label}: {r.value_text} (target {r.target_text})"
                 for r in verdict.results]
        out.append(Card(verdict.question, verdict.title, verdict.status, headline, subhead, sentence, lines))
    return out
