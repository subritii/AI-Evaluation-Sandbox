"""Acceptance criteria: judge every headline number against a target from acceptance.toml.

Pure logic (no Streamlit), so the dashboard, the HTML report, and the tests
all use the same rules. Measurements come only from saved reports; a
criterion with no report behind it is "no data", never a guess.

Most criteria are measured on the reports selected in the dashboard. A
criterion scoped to a `configuration` (e.g. native GPU vs isolated all-Docker
CPU) is measured on the largest saved latency run of that configuration, so
each configuration gets its own row instead of whichever run happens to be
selected.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from reports import backend_label, backend_of, headline_latency

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
LATENCY_METRICS = {"pii_scan_p95_ms": "pii_scan", "total_p95_ms": "total"}


@dataclass(frozen=True)
class Configuration:
    id: str
    label: str
    model_backend: str
    providers: tuple[str, ...] = ("ollama",)
    headline: bool = False


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
    configuration: str | None = None


@dataclass(frozen=True)
class Config:
    prepared_for: str
    criteria: list[Criterion]
    baseline_report: str | None = None
    baseline_label: str = ""
    configurations: dict[str, Configuration] = field(default_factory=dict)

    def headline_configuration(self) -> Configuration | None:
        return next((c for c in self.configurations.values() if c.headline), None)


@dataclass(frozen=True)
class Measurement:
    value: float | int | str
    source: str  # which run, in words
    denominator: int | None = None  # e.g. canaries planted
    run: dict | None = None  # the latency run, for configuration-scoped criteria


@dataclass(frozen=True)
class Result:
    criterion: Criterion
    measurement: Measurement | None
    status: str
    configuration: Configuration | None = None

    @property
    def label(self) -> str:
        """Criterion label, with its configuration when it has one."""
        return f"{self.criterion.label} · {self.configuration.label}" if self.configuration else self.criterion.label

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
    """Read and validate acceptance.toml; a malformed entry is an error, not a silent skip."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    configurations = {}
    for item in raw.get("configurations", []):
        conf = Configuration(**(item | {"providers": tuple(item.get("providers", ("ollama",)))}))
        if conf.id in configurations:
            raise ValueError(f"configuration ids must be unique: {conf.id}")
        configurations[conf.id] = conf
    if sum(c.headline for c in configurations.values()) > 1:
        raise ValueError("at most one configuration can be the headline")
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
        if c.configuration is not None:
            if c.configuration not in configurations:
                raise ValueError(f"criterion {c.id}: unknown configuration {c.configuration!r}")
            if c.metric not in LATENCY_METRICS:
                raise ValueError(f"criterion {c.id}: only latency metrics can be scoped to a configuration")
        criteria.append(c)
    if len({c.id for c in criteria}) != len(criteria):
        raise ValueError("criterion ids must be unique")
    baseline = raw.get("baseline", {})
    return Config(raw.get("prepared_for", ""), criteria, baseline.get("report"), baseline.get("label", ""),
                  configurations)


def format_value(fmt: str, m: Measurement) -> str:
    v = m.value
    if fmt == "percent":
        return f"{v:.1%}"
    if fmt == "ms":
        return f"{v:,.0f} ms"
    if fmt == "count_of_planted":
        return f"{v}/{m.denominator}" if m.denominator is not None else str(v)
    return str(v)


# --- Measurements -----------------------------------------------------------------

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
                           f"canary audit {can['run_id']}, {can['requests']} requests, {backend_label(backend_of(can))}",
                           can["planted"]["total"])
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


METRICS = {
    "answer_accuracy": _answer_accuracy,
    "detector_recall": _detector_recall,
    "canaries_unmasked": _canary("unmasked"),
    "canaries_downstream": _canary("downstream"),
    "airgap_verdict": _airgap,
    "pii_scan_p95_ms": _latency("pii_scan"),
    "total_p95_ms": _latency("total"),
}


def run_matches(run: dict, conf: Configuration) -> bool:
    """A run belongs to a configuration when its only backend and all its providers match."""
    return run["model_backends"] == [conf.model_backend] and set(run["providers"]) <= set(conf.providers)


def configuration_run(runs: list[dict], conf: Configuration) -> dict | None:
    """The largest saved latency run of a configuration; `runs` is newest first, so a tie keeps the newest."""
    matching = [r for r in runs if run_matches(r, conf)]
    return max(matching, key=lambda r: r["n"]) if matching else None


def _run_label(run: dict) -> str:
    kind = "canary audit" if run["kind"] == "canary_audit" else "dashboard batch"
    return f"n={run['n']}, {kind} {run['run_id']}"


def measure(c: Criterion, summaries: dict, config: Config) -> Measurement | None:
    if c.configuration is None:
        return METRICS[c.metric](summaries)
    run = configuration_run(summaries.get("latency_runs") or [], config.configurations[c.configuration])
    stage = (run or {}).get("latency", {}).get("stages", {}).get(LATENCY_METRICS[c.metric]) if run else None
    if not stage:
        return None
    return Measurement(stage["p95"], _run_label(run), run=run)


def measure_all(config: Config, summaries: dict) -> dict[str, Measurement | None]:
    """Measurement per criterion id. summaries: batch, rag, det, can, airgap, latency_runs (any may be None)."""
    return {c.id: measure(c, summaries, config) for c in config.criteria}


def evaluate(config: Config, measurements: dict[str, Measurement | None]) -> list[Result]:
    """Status per criterion; `measurements` is keyed by criterion id."""
    results = []
    for c in config.criteria:
        m = measurements.get(c.id)
        if m is None:
            status = NO_DATA
        elif COMPARISONS[c.comparison](m.value, c.target):
            status = PASS
        else:
            status = KNOWN_GAP if c.known_gap.strip() else FAIL
        results.append(Result(c, m, status, config.configurations.get(c.configuration) if c.configuration else None))
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
