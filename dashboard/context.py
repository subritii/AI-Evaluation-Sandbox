"""Everything the pages need, loaded once per rerun from the selected reports.

No page reads report files itself: they all get the same Context, so every
page, the Overview verdicts, and the HTML report agree on which runs are shown.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import criteria
import reports

KINDS = ("dashboard_batch", "eval_rag", "eval_detector", "canary_audit", "airgap_check")
KIND_LABELS = {
    "dashboard_batch": "Batch run",
    "eval_rag": "RAG eval",
    "eval_detector": "Detector eval",
    "canary_audit": "Canary audit",
    "airgap_check": "Air-gap check",
}


@dataclass
class Context:
    reports_dir: Path
    api: str
    info: dict | None  # gateway /info, None if unreachable
    paths: dict[str, Path | None]  # selected report per kind
    batch: dict | None  # raw dashboard batch record
    rag: dict | None
    det: dict | None
    can: dict | None
    airgap: dict | None
    config: criteria.Config
    baseline: dict | None  # summarized baseline eval, if its report exists
    results: list[criteria.Result] = field(default_factory=list)
    verdicts: list[criteria.Verdict] = field(default_factory=list)
    # st.Page objects by key, for links between pages (empty in tests).
    pages: dict = field(default_factory=dict)

    def summaries(self) -> dict:
        return {"batch": self.batch, "rag": self.rag, "det": self.det, "can": self.can, "airgap": self.airgap}

    def results_for(self, question: str) -> list[criteria.Result]:
        return [r for r in self.results if r.criterion.question == question]


def default_paths(reports_dir: Path) -> dict[str, Path | None]:
    return {kind: reports.default_report(kind, reports.list_reports(reports_dir, kind)) for kind in KINDS}


def load_context(reports_dir: Path, api: str, info: dict | None, paths: dict[str, Path | None],
                 config: criteria.Config | None = None) -> Context:
    config = config or criteria.load_config()

    def summarize(kind, fn):
        path = paths.get(kind)
        return fn(path, reports.load(path)) if path else None

    baseline = None
    if config.baseline_report and (reports_dir / config.baseline_report).exists():
        path = reports_dir / config.baseline_report
        baseline = reports.summarize_eval_rag(path, reports.load(path))
    ctx = Context(
        reports_dir=reports_dir, api=api, info=info, paths=paths,
        batch=reports.load(paths["dashboard_batch"]) if paths.get("dashboard_batch") else None,
        rag=summarize("eval_rag", reports.summarize_eval_rag),
        det=summarize("eval_detector", reports.summarize_detector),
        can=summarize("canary_audit", reports.summarize_canary),
        airgap=summarize("airgap_check", reports.summarize_airgap),
        config=config, baseline=baseline,
    )
    ctx.results = criteria.evaluate(config, criteria.measure_all(ctx.summaries()))
    ctx.verdicts = criteria.verdicts(ctx.results)
    return ctx


def run_date(started_at: str | None) -> str:
    if not started_at:
        return "date not recorded"
    try:
        return datetime.fromisoformat(started_at).strftime("%Y-%m-%d")
    except ValueError:
        return started_at[:10]


def runs_shown(ctx: Context) -> list[str]:
    """One plain line per selected run: what it is, its size, backend, and date."""
    lines = []
    if ctx.rag:
        lines.append(f"RAG eval: {ctx.rag['questions']} questions, {reports.backend_label(ctx.rag['model_backend'])}, "
                     f"{run_date(ctx.rag['source']['started_at'])}")
    if ctx.det:
        records = (ctx.det.get("dataset") or {}).get("records")
        lines.append(f"Detector eval: {records:,} synthetic records, {run_date(ctx.det['source']['started_at'])}"
                     if records else f"Detector eval: {run_date(ctx.det['source']['started_at'])}")
    if ctx.can:
        lines.append(f"Canary audit: {ctx.can['requests']} requests, {reports.backend_label(reports.backend_of(ctx.can))}, "
                     f"{run_date(ctx.can['source']['started_at'])}")
    if ctx.airgap:
        lines.append(f"Air-gap check: {ctx.airgap['verdict']}, {reports.backend_label(ctx.airgap['model_backend'])}, "
                     f"{run_date(ctx.airgap['source']['started_at'])}")
    if ctx.batch:
        gw = ctx.batch.get("gateway") or {}
        lines.append(f"Batch run: {ctx.batch['input']['questions']} questions, "
                     f"{reports.backend_label(gw.get('model_backend'))}, {run_date(ctx.batch.get('started_at'))}")
    return lines
