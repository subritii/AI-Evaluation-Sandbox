"""Acceptance criteria: judge every headline number against a target from acceptance.toml.

Pure logic (no Streamlit), so the dashboard, the HTML report, and the tests
all use the same rules. Measurements come only from the selected reports;
a criterion with no report behind it is "no data", never a guess.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from reports import backend_label, headline_latency

CONFIG_PATH = Path(__file__).with_name("acceptance.toml")

PASS, KNOWN_GAP, FAIL, NO_DATA = "pass", "known gap", "fail", "no data"
# Worst first: a question's verdict is its worst criterion.
SEVERITY = [FAIL, NO_DATA, KNOWN_GAP, PASS]
STATUS_ICON = {PASS: "✅", KNOWN_GAP: "⚠️", FAIL: "❌", NO_DATA: "⏳"}
STATUS_LABEL = {PASS: "Pass", KNOWN_GAP: "Known gap", FAIL: "Fail", NO_DATA: "No data"}

# The buyer's questions, in page order.
QUESTIONS = {
    "accuracy": "Does it answer correctly?",
    "privacy": "Does PII stay protected?",
    "isolation": "Is it cut off from the internet?",
    "performance": "Is it fast enough?",
}
COMPARISONS = {">=": lambda v, t: v >= t, "<=": lambda v, t: v <= t, "==": lambda v, t: v == t}
FORMATS = {"percent", "count_of_planted", "text", "ms"}


@dataclass(frozen=True)
class Criterion:
    id: str
    question: str
    label: str
    metric: str
    comparison: str
    target: float | int | str
    format: str
    known_gap: str = ""


@dataclass(frozen=True)
class Config:
    prepared_for: str
    criteria: list[Criterion]
    baseline_report: str | None = None
    baseline_label: str = ""


@dataclass(frozen=True)
class Measurement:
    value: float | int | str
    source: str  # which run: e.g. "canary audit canary-…, n=200, native Ollama (host, Apple GPU)"
    denominator: int | None = None  # e.g. canaries planted


@dataclass(frozen=True)
class Result:
    criterion: Criterion
    measurement: Measurement | None
    status: str

    @property
    def value_text(self) -> str:
        return "—" if self.measurement is None else format_value(self.criterion.format, self.measurement)

    @property
    def target_text(self) -> str:
        c = self.criterion
        shown = format_value(c.format, Measurement(c.target, "")) if c.format != "count_of_planted" else str(c.target)
        return f"{c.comparison} {shown}".replace(">=", "≥").replace("<=", "≤").replace("==", "=")

    @property
    def note(self) -> str:
        return self.criterion.known_gap if self.status == KNOWN_GAP else ""


def load_config(path: Path = CONFIG_PATH) -> Config:
    """Read and validate acceptance.toml; a malformed criterion is an error, not a silent skip."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    criteria = []
    for item in raw.get("criteria", []):
        c = Criterion(**item)
        if c.question not in QUESTIONS:
            raise ValueError(f"criterion {c.id}: unknown question {c.question!r}")
        if c.comparison not in COMPARISONS:
            raise ValueError(f"criterion {c.id}: unknown comparison {c.comparison!r}")
        if c.format not in FORMATS:
            raise ValueError(f"criterion {c.id}: unknown format {c.format!r}")
        if c.metric not in METRICS:
            raise ValueError(f"criterion {c.id}: unknown metric {c.metric!r}")
        criteria.append(c)
    if len({c.id for c in criteria}) != len(criteria):
        raise ValueError("criterion ids must be unique")
    baseline = raw.get("baseline", {})
    return Config(raw.get("prepared_for", ""), criteria, baseline.get("report"), baseline.get("label", ""))


def format_value(fmt: str, m: Measurement) -> str:
    v = m.value
    if fmt == "percent":
        return f"{v:.1%}"
    if fmt == "ms":
        return f"{v:,.0f} ms"
    if fmt == "count_of_planted":
        return f"{v}/{m.denominator}" if m.denominator is not None else str(v)
    return str(v)


# --- Measurements from the selected reports -------------------------------------

def _answer_accuracy(s: dict) -> Measurement | None:
    rag = s.get("rag")
    if not rag or rag["answers_correct"] is None or not rag["answerable"]:
        return None
    return Measurement(rag["answers_correct"] / rag["answerable"],
                       f"RAG eval {rag['source']['file']}, {rag['answers_correct']}/{rag['answerable']} questions, "
                       f"{backend_label(rag['model_backend'])}")


def _detector_recall(s: dict) -> Measurement | None:
    det = s.get("det")
    if not det:
        return None
    o = det["overall"]
    return Measurement(o["recall"], f"detector eval {det['source']['file']}, {o['support']:,} labeled spans")


def _canary(group: str):
    def measure(s: dict) -> Measurement | None:
        can = s.get("can")
        if not can:
            return None
        return Measurement(can[group]["total"],
                           f"canary audit {can['run_id']}, {can['requests']} requests, "
                           f"{backend_label(_backend_of_models(can))}", can["planted"]["total"])
    return measure


def _airgap(s: dict) -> Measurement | None:
    air = s.get("airgap")
    if not air:
        return None
    return Measurement(air["verdict"], f"air-gap check {air['source']['file']}, {backend_label(air['model_backend'])}")


def _latency(stage: str):
    def measure(s: dict) -> Measurement | None:
        headline = headline_latency(s.get("batch"), s.get("can"))
        if not headline or stage not in headline[0]["stages"]:
            return None
        latency, label = headline
        return Measurement(latency["stages"][stage]["p95"], label)
    return measure


def _backend_of_models(can: dict) -> str | None:
    backends = {m["model_backend"] for m in can.get("models") or []}
    return backends.pop() if len(backends) == 1 else ("mixed" if backends else None)


METRICS = {
    "answer_accuracy": _answer_accuracy,
    "detector_recall": _detector_recall,
    "canaries_unmasked": _canary("unmasked"),
    "canaries_downstream": _canary("downstream"),
    "airgap_verdict": _airgap,
    "pii_scan_p95_ms": _latency("pii_scan"),
    "total_p95_ms": _latency("total"),
}


def measure_all(summaries: dict) -> dict[str, Measurement | None]:
    """summaries: {"batch", "rag", "det", "can", "airgap"} (any may be None)."""
    return {name: fn(summaries) for name, fn in METRICS.items()}


def evaluate(config: Config, measurements: dict[str, Measurement | None]) -> list[Result]:
    results = []
    for c in config.criteria:
        m = measurements.get(c.metric)
        if m is None:
            status = NO_DATA
        elif COMPARISONS[c.comparison](m.value, c.target):
            status = PASS
        else:
            status = KNOWN_GAP if c.known_gap.strip() else FAIL
        results.append(Result(c, m, status))
    return results


def worst(statuses: list[str]) -> str:
    return min(statuses, key=SEVERITY.index) if statuses else NO_DATA


@dataclass
class Verdict:
    question: str
    title: str
    status: str
    results: list[Result] = field(default_factory=list)


def verdicts(results: list[Result]) -> list[Verdict]:
    """One verdict per buyer question: the worst status among its criteria."""
    out = []
    for key, title in QUESTIONS.items():
        mine = [r for r in results if r.criterion.question == key]
        out.append(Verdict(key, title, worst([r.status for r in mine]), mine))
    return out
