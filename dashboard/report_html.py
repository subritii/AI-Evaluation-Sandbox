"""Build the downloadable HTML report: results plus the methodology behind them.

The report is one self-contained file (inline CSS, no scripts, no external
links), so it opens offline and can be emailed as-is. It contains no question
text, masked or not: masking can miss PII (the canary audit measures how
often), so only counts, rates, and timings appear.

Every section names the run and file it came from. Sections whose run is
missing say so instead of showing placeholder numbers.
"""

from datetime import datetime, timezone
from html import escape

from batch import STAGES
from reports import backend_label, backend_of, headline_latency, latency_n

_backend_label = backend_label

STAGE_NOTES = {
    "pii_scan": "Trust Engine (Presidio + custom recognizers) detects and masks the question",
    "retrieval": "embed the masked question (Ollama) + pgvector similarity search",
    "time_to_first_token": "LLM request sent → first generated token",
    "generation": "first token → last token",
    "total": "request start → citations attached",
}

# Documented in docs/build-log.md; each was measured, not assumed.
KNOWN_GAPS = [
    "Bare digit strings with no context words (\"Send the funds to 080154303 / 38299737631\") are not masked "
    "as routing or account numbers. Deliberate: flagging every 6-17 digit number would mask order IDs and "
    "ticket numbers everywhere. This is most of the canary leakage.",
    "Some names are missed by spaCy NER, mostly non-English names.",
    "Abbreviations such as \"A/C\" (account) and \"bank code\" (routing) are often not recognized as context.",
    "A bare phone number after \"call\" is not masked (\"call\" is a spaCy stop word, so it can't act as context).",
    "The detector dataset, canary templates, and RAG questions were written by the same person who wrote the "
    "recognizers and policy. Results are optimistic and catch regressions; they don't estimate accuracy on real "
    "bank text.",
    "The RAG eval has 11 questions: useful for regressions, not a statistically meaningful accuracy estimate.",
    "llama3.2:3b sometimes quotes a rule without applying it (threshold comparisons, \"delete\" vs \"never store\").",
    "Latency is from one machine, one request at a time. Native Ollama (Apple GPU) and Docker Ollama (CPU on "
    "macOS) differ by about an order of magnitude; each number below names its backend.",
]

CSS = """
:root { --fg:#1f2328; --muted:#59636e; --line:#d1d9e0; --bg:#ffffff; --panel:#f6f8fa;
        --accent:#0969da; --p95:#9a6700; --warn-bg:#fff8c5; --warn-line:#d4a72c; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e6edf3; --muted:#9198a1; --line:#3d444d; --bg:#0d1117; --panel:#151b23;
          --accent:#4493f8; --p95:#d29922; --warn-bg:#2e2611; --warn-line:#9e6a03; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
main { max-width:960px; margin:0 auto; padding:32px 16px 64px; }
h1 { font-size:26px; margin:0 0 4px; } h2 { font-size:20px; margin:40px 0 8px; padding-top:8px;
     border-top:1px solid var(--line); } h3 { font-size:16px; margin:20px 0 6px; }
.muted, .source { color:var(--muted); font-size:13px; }
.source code { font-size:12px; }
table { border-collapse:collapse; width:100%; margin:8px 0 12px; font-size:14px; }
th, td { border-bottom:1px solid var(--line); padding:5px 8px; text-align:left; vertical-align:top; }
th { background:var(--panel); font-weight:600; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(190px, 1fr)); gap:12px; margin:16px 0; }
.tile { border:1px solid var(--line); border-radius:8px; padding:12px; background:var(--panel); }
.tile .v { font-size:22px; font-weight:650; font-variant-numeric:tabular-nums; }
.tile .l { font-size:13px; color:var(--muted); }
.warn { background:var(--warn-bg); border:1px solid var(--warn-line); border-radius:6px; padding:8px 12px; margin:8px 0; }
.bar { position:relative; height:14px; min-width:160px; background:var(--panel); border-radius:3px; }
.bar span { position:absolute; left:0; top:0; bottom:0; border-radius:3px; }
.bar .p95 { background:var(--p95); opacity:.55; } .bar .p50 { background:var(--accent); }
.legend span { display:inline-block; width:10px; height:10px; border-radius:2px; margin:0 4px 0 12px; }
code { font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:13px; }
@media (max-width:600px) { .wide { display:block; overflow-x:auto; } }
"""


def _e(value) -> str:
    return escape("—" if value is None else str(value))


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.1%}"


def _ms(x: float | None) -> str:
    return "—" if x is None else f"{x:,.0f}"


def _ratio(a, b) -> str:
    return "—" if a is None or b is None else f"{a}/{b}"


def _load(values) -> str:
    """'2.2 → 4.1' from [[1m,5m,15m], [1m,5m,15m]] (1-minute load before and after)."""
    if not values or any(v is None for v in values):
        return "not recorded"
    return f"{values[0][0]:.1f} → {values[1][0]:.1f}"


def _source_line(source: dict, extra: str = "") -> str:
    return (
        f'<p class="source">Source: <code>reports/{_e(source["file"])}</code> · started {_e(source["started_at"])}'
        f' · commit <code>{_e(source["git_commit"])}</code>{extra}</p>'
    )


def _latency_table(stages: dict) -> str:
    rows = []
    scale = max((s["p95"] for s in stages.values()), default=1) or 1
    for stage in STAGES:
        s = stages.get(stage)
        if not s:
            continue
        bar = (
            f'<div class="bar" title="P50 {_ms(s["p50"])} ms, P95 {_ms(s["p95"])} ms">'
            f'<span class="p95" style="width:{100 * s["p95"] / scale:.1f}%"></span>'
            f'<span class="p50" style="width:{100 * s["p50"] / scale:.1f}%"></span></div>'
        )
        rows.append(
            f"<tr><td><b>{stage}</b><br><span class='muted'>{STAGE_NOTES[stage]}</span></td>"
            f"<td class='num'>{s['n']}</td><td class='num'>{_ms(s['avg'])}</td><td class='num'>{_ms(s['p50'])}</td>"
            f"<td class='num'>{_ms(s['p90'])}</td><td class='num'>{_ms(s['p95'])}</td>"
            f"<td class='num'>{_ms(s['p99'])}</td><td>{bar}</td></tr>"
        )
    return (
        "<div class='wide'><table><tr><th>Stage</th><th class='num'>n</th><th class='num'>avg</th>"
        "<th class='num'>P50</th><th class='num'>P90</th><th class='num'>P95</th><th class='num'>P99</th>"
        "<th>P50 / P95</th></tr>" + "".join(rows) + "</table></div>"
        "<p class='legend muted'>Milliseconds. Bars share one scale."
        "<span style='background:var(--accent)'></span>P50<span style='background:var(--p95);opacity:.55'></span>P95</p>"
    )


def combine_notes(notes: list[str]) -> list[str]:
    """Merge the gateway's per-stage small-sample notes into one line when they all say the same thing."""
    small = [n for n in notes if "P99 is not reliable" in n]
    if len(small) > 1:
        counts = {n.split("only ")[1].split(" samples")[0] for n in small}
        if len(counts) == 1:
            threshold = small[0].rsplit("below ", 1)[1].rstrip(".")
            merged = f"only {counts.pop()} samples per stage; P99 is not reliable below {threshold}."
            return [merged] + [n for n in notes if n not in small]
    return notes


def _notes(notes: list[str]) -> str:
    return "".join(f"<p class='muted'>Note: {_e(n)}</p>" for n in combine_notes(notes or []))


def _batch_section(batch: dict | None) -> str:
    if not batch:
        return "<h2>1. Batch run</h2><p class='muted'>No batch run selected.</p>"
    metrics = batch.get("latency") or {}
    gw = batch.get("gateway") or {}
    status = ", ".join(f"{k}: {v}" for k, v in sorted(batch["status_counts"].items()))
    entities = ", ".join(f"{k} {v}" for k, v in sorted(batch["masked_entity_totals"].items())) or "none"
    body = (
        f"<h2>1. Batch run <code>{_e(batch['run_id'])}</code></h2>"
        f"<p class='source'>Input: <code>{_e(batch['input']['file_name'])}</code> · sha256 "
        f"<code>{_e(batch['input']['sha256'][:16])}…</code> · {batch['input']['questions']} questions · "
        f"{batch['input']['warmup']} warmup requests excluded · started {_e(batch['started_at'])} · "
        f"backend: <b>{_e(_backend_label(gw.get('model_backend')))}</b>, {_e(gw.get('llm_model'))}</p>"
        f"<p>HTTP status: {_e(status)}. Refusals: {batch['refused']}. Entities masked in questions: {_e(entities)}.</p>"
    )
    if metrics.get("stages"):
        body += _latency_table(metrics["stages"]) + _notes(metrics.get("notes"))
    else:
        body += "<p class='warn'>The gateway returned no latency samples for this run (no request succeeded).</p>"
    return body


def _rag_section(rag: dict | None) -> str:
    if not rag:
        return "<h2>2. RAG answer quality</h2><p class='muted'>No RAG eval report found.</p>"
    rows = "".join(
        f"<tr><td>{_e(r['id'])}</td><td>{r['type']}</td><td>{_pf(r['retrieval'])}</td><td>{_pf(r['answer'])}</td>"
        f"<td>{_pf(r['citation'])}</td><td class='num'>{_ms(r['total_ms'])}</td></tr>"
        for r in rag["rows"]
    )
    prec = "—" if rag["citation_precision"] is None else f"{rag['citation_precision']:.0%}"
    return (
        "<h2>2. RAG answer quality</h2>"
        + _source_line(rag["source"], f" · backend: <b>{_e(_backend_label(rag['model_backend']))}</b>, {_e(rag['llm_model'])}")
        + "<table><tr><th>Metric</th><th class='num'>Result</th></tr>"
        f"<tr><td>Expected chunk retrieved in top k</td><td class='num'>{rag['retrieval_hits']}/{rag['answerable']}"
        f" ({rag['top1']} at rank 1)</td></tr>"
        f"<tr><td>Correct answers (keywords present, no hedging)</td><td class='num'>{_ratio(rag['answers_correct'], rag['answerable'])}</td></tr>"
        f"<tr><td>Correct refusals (out-of-scope questions)</td><td class='num'>{_ratio(rag['refusals_correct'], rag['refusal_cases'])}</td></tr>"
        f"<tr><td>Evidence chunk cited (code-attached citations)</td><td class='num'>{_ratio(rag['citations_hit'], rag['answerable'])}"
        f", mean precision {prec}</td></tr></table>"
        "<div class='wide'><table><tr><th>Question</th><th>Type</th><th>Retrieval</th><th>Answer</th><th>Citation</th>"
        f"<th class='num'>total ms</th></tr>{rows}</table></div>"
    )


def _pf(value) -> str:
    return "—" if value is None else ("PASS" if value else "<b>FAIL</b>")


def _detector_section(det: dict | None) -> str:
    if not det:
        return "<h2>3. PII detector accuracy</h2><p class='muted'>No detector eval report found.</p>"
    rows = "".join(
        f"<tr><td>{_e(name)}</td><td class='num'>{m['support']}</td><td class='num'>{_pct(m['precision'])}</td>"
        f"<td class='num'>{_pct(m['recall'])}</td><td class='num'>{m['tp']}</td><td class='num'>{m['fp']}</td>"
        f"<td class='num'>{m['fn']}</td></tr>"
        for name, m in det["per_entity"].items()
    )
    o = det["overall"]
    rows += (
        f"<tr><th>Overall (micro)</th><th class='num'>{o['support']}</th><th class='num'>{_pct(o['precision'])}</th>"
        f"<th class='num'>{_pct(o['recall'])}</th><th class='num'>{o['tp']}</th><th class='num'>{o['fp']}</th>"
        f"<th class='num'>{o['fn']}</th></tr>"
    )
    diff = det["by_difficulty"]
    split = "".join(
        f"<li>{label}: recall {_pct(diff[k]['recall'])}, precision {_pct(diff[k]['precision'])} ({diff[k]['support']} spans)</li>"
        for k, label in (("standard", "Standard templates (clear context)"), ("hard", "Hard templates (no context, abbreviations)"))
        if k in diff
    )
    neg = det["negatives"] or {}
    ds = det["dataset"] or {}
    return (
        "<h2>3. PII detector accuracy</h2>"
        + _source_line(det["source"], f" · scrubber <code>{_e(det['scrubber'])}</code>")
        + f"<p class='muted'>{_e(ds.get('records'))} synthetic records (sha256 <code>{_e(str(ds.get('sha256'))[:16])}…</code>), "
        f"threshold {_e(det['threshold'])}. A gold span counts as found when a prediction of the same type overlaps it. "
        "For privacy, recall matters most: a miss leaks, a false positive only over-masks.</p>"
        "<div class='wide'><table><tr><th>Entity</th><th class='num'>Support</th><th class='num'>Precision</th>"
        f"<th class='num'>Recall</th><th class='num'>TP</th><th class='num'>FP</th><th class='num'>FN</th></tr>{rows}</table></div>"
        f"<ul>{split}<li>No-PII records with any detection: {_e(neg.get('flagged'))} of {_e(neg.get('total'))}</li></ul>"
    )


def _canary_section(can: dict | None) -> str:
    if not can:
        return "<h2>4. Canary leakage audit</h2><p class='muted'>No canary audit report found.</p>"
    planted, found = can["planted"], can["found"]
    by_loc = found.get("by_location") or {}
    searched = can.get("searched") or {}
    locations = [f"response.{x}" for x in searched.get("responses", [])] + [f"logs.{x}" for x in searched.get("logs", [])]
    loc_rows = "".join(
        f"<tr><td>{_e(loc)}</td><td class='num'>{sum(by_loc.get(loc, {}).values())}</td></tr>" for loc in locations
    )
    db_hits = sum(sum(v.values()) for k, v in by_loc.items() if k.startswith("database."))
    loc_rows += (
        f"<tr><td>database ({len(searched.get('database_columns', []))} text columns)</td><td class='num'>{db_hits}</td></tr>"
    )
    if can["not_searched"] is None:
        gaps = "<p class='warn'>This report predates the not-searched check; log coverage was not verified.</p>"
    elif can["not_searched"]:
        gaps = "".join(f"<p class='warn'>Not searched: {_e(x)}</p>" for x in can["not_searched"])
    else:
        gaps = "<p class='muted'>Every listed location was searched.</p>"
    latency = can.get("latency") or {}
    latency_html = (
        f"<h3>Per-stage latency of this batch</h3>{_latency_table(latency['stages'])}{_notes(latency.get('notes'))}"
        if latency.get("stages") else ""
    )
    unmasked, downstream = can["unmasked"], can["downstream"]
    return (
        "<h2>4. Canary leakage audit</h2>"
        + _source_line(can["source"], f" · run <code>{_e(can['run_id'])}</code> · backend: <b>{_e(_backend_label(backend_of(can)))}</b>")
        + f"<p>{planted['total']} synthetic canaries were planted across {can['requests']} requests. Two different "
        "questions, answered separately:</p><ul>"
        f"<li><b>Unmasked by the Trust Engine: {unmasked['total']}/{planted['total']}.</b> Found in "
        "<code>response.masked_question</code>, the text sent to the embedding model and the local LLM. These are "
        "detector misses; the models received the value.</li>"
        f"<li><b>Found in answers, logs, or storage: {downstream['total']}/{planted['total']}.</b> Found in an answer, "
        "another response field, a searched log, or any database text column.</li></ul>"
        "<div class='wide'><table><tr><th>Entity</th><th class='num'>Planted</th><th class='num'>Unmasked</th>"
        "<th class='num'>In answers, logs, or storage</th></tr>"
        + "".join(
            f"<tr><td>{_e(e)}</td><td class='num'>{n}</td><td class='num'>{unmasked['by_entity'].get(e, 0)}</td>"
            f"<td class='num'>{downstream['by_entity'].get(e, 0)}</td></tr>"
            for e, n in sorted(planted["by_entity"].items())
        )
        + f"</table></div><h3>Where canaries were found</h3><table><tr><th>Location</th><th class='num'>Canaries found</th></tr>"
        f"{loc_rows}</table>{gaps}{latency_html}"
    )


def _model_line(gw: dict, kind: str) -> str | None:
    """'llama3.2:3b via ollama at http://ollama:11434'; gateways before Task 9 report the model only."""
    model = gw.get(f"{kind}_model")
    if not model or not gw.get(f"{kind}_provider"):
        return model
    return f"{model} via {gw[f'{kind}_provider']} at {gw.get(f'{kind}_endpoint')}"


def _isolation_statement(backend: str | None, airgap: dict | None = None) -> str:
    """What is and isn't proven about network access, citing the air-gap check when one is selected."""
    text = ("No external services are called: embeddings and generation go to a local Ollama, and there are no "
            "cloud APIs or telemetry. ")
    if airgap and airgap["verdict"] == "PASS" and airgap["model_backend"] == "docker":
        src = airgap["source"]
        text += (
            "In the all-Docker configuration, network isolation is enforced by Docker and was tested by "
            f"<code>scripts/airgap_check.py</code> (<code>reports/{_e(src['file'])}</code>, {_e(src['started_at'])}, "
            f"commit <code>{_e(src['git_commit'])}</code>, verdict <b>PASS</b>). The containers that handle data "
            f"({_e(', '.join(airgap['isolated']))}) are on an internal network with no route out: connections to "
            "public IPs, DNS lookups, and the Ollama model registry failed from inside them, while the same probe on "
            "a normal Docker network connected (the control). A real query succeeded through the isolated stack. "
            "The ingress proxy is the one container with a route out, because Docker can only publish ports from a "
            "non-internal network; it holds no data and runs a static, read-only nginx config."
        )
    elif airgap:
        text += (
            f"The selected air-gap check (<code>reports/{_e(airgap['source']['file'])}</code>) did not pass: verdict "
            f"<b>{_e(airgap['verdict'])}</b>, backend {_e(airgap['model_backend'])}. Isolation is not established for it."
        )
    else:
        text += ("Network isolation is enforced only in the all-Docker configuration and is tested by "
                 "<code>scripts/airgap_check.py</code>; no air-gap check report was selected.")
    if backend == "native":
        text += " This batch used native Ollama on the host, which is outside that isolation."
    elif backend == "remote":
        text += (" This batch used a model endpoint on another machine, outside that isolation: masked questions "
                 "were sent to it, and masking misses some PII (see the canary audit).")
    elif backend == "docker":
        text += " This batch used the all-Docker configuration."
    return text


def _methodology(batch, rag, det, can, airgap=None) -> str:
    gw = (batch or {}).get("gateway") or {}
    env = (batch or {}).get("environment") or {}
    config_rows = "".join(
        f"<tr><td>{label}</td><td>{_e(value)}</td></tr>"
        for label, value in (
            ("Model backend", _backend_label(gw.get("model_backend")) if batch else None),
            ("LLM", _model_line(gw, "llm")),
            ("Embedding model", _model_line(gw, "embed")),
            ("Temperature / max tokens", f"{gw.get('temperature')} / {gw.get('max_tokens')}" if gw else None),
            ("Trust Engine", gw.get("scrubber")),
        )
    )
    batch_backend = backend_of({"model_backend": gw.get("model_backend")}) if batch else None
    mismatches = [
        f"The {name} ran on {_backend_label(b)}, but the batch ran on {_backend_label(batch_backend)}; "
        "compare its latency with care."
        for name, b in (("RAG eval", backend_of(rag)), ("canary audit", backend_of(can)))
        if batch_backend and b and b != batch_backend
    ]
    load_rows = []
    if batch:
        load_rows.append(
            f"<tr><td>Batch run (load where the gateway runs: the Docker VM on macOS, {_e(env.get('gateway_cpus'))} CPUs)</td>"
            f"<td>{_load([env.get('gateway_load_avg_before'), env.get('gateway_load_avg_after')])}</td></tr>"
        )
        if gw.get("model_backend") == "native":
            load_rows.append(
                "<tr><td colspan='2' class='muted'>With native Ollama the models run on the Mac, outside that VM, so "
                "this figure does not include model load. The host scripts below record the Mac's own load.</td></tr>"
            )
    if rag:
        load_rows.append(f"<tr><td>RAG eval (host)</td><td>{_load(rag['host_load_avg'])}</td></tr>")
    if can:
        load_rows.append(f"<tr><td>Canary audit (host)</td><td>{_load(can['host_load_avg'])}</td></tr>")
    gaps = list(KNOWN_GAPS)
    if can and can.get("not_searched"):
        gaps.insert(0, "Canary audit locations not searched in this run: " + "; ".join(can["not_searched"]) + ".")
    return (
        "<h2>5. Methodology</h2>"
        "<h3>Pipeline</h3><p>Each question goes through the gateway's <code>POST /query</code>: the Trust Engine masks "
        "PII with typed placeholders, the masked question is embedded (Ollama) and matched against policy chunks in "
        "pgvector (chunks were masked before embedding), a local LLM answers from the retrieved chunks, and citations "
        "are attached by code from the retrieved chunks, never written by the model.</p>"
        f"<p>{_isolation_statement(gw.get('model_backend') if batch else None, airgap)}</p>"
        f"<h3>Configuration (reported by the gateway at run time)</h3><table>{config_rows}</table>"
        + "".join(f"<p class='warn'>{_e(m)}</p>" for m in mismatches)
        + "<h3>Latency</h3><ul>"
        "<li>Timed inside the gateway with <code>time.perf_counter()</code>, per stage, and stored per request in "
        "Postgres (<code>latency_samples</code>, with the model backend and model names on every row).</li>"
        "<li>Requests are sent one at a time, so the numbers are pipeline time, not queueing. Warmup requests "
        "(a fixed PII-free question, separate run id) load the models first and are excluded.</li>"
        "<li>Percentiles use numpy's default linear interpolation over all samples of a run. With n samples, P99 "
        "rests on about n/100 requests: below 100 samples it is not reliable, and the table says so.</li>"
        "</ul><h3>Machine load</h3><p>Latency depends on what else the machine is doing. 1-minute load average "
        "before → after each run:</p>"
        f"<table>{''.join(load_rows) or '<tr><td>No runs selected</td></tr>'}</table>"
        "<h3>Accuracy and leakage</h3><ul>"
        "<li>Detector accuracy: the production <code>detect()</code> over a labeled synthetic dataset (Faker, fixed "
        "seed); precision and recall per entity type.</li>"
        "<li>Canary audit: unique synthetic PII values planted in about half the requests of a batch, then searched "
        "for (exact and separator-insensitive) in API responses, backend/db/Ollama logs, and every text column in "
        "the database. You can't prove a negative, but you can check for known planted values everywhere data "
        "could land.</li>"
        "<li>RAG eval: labeled questions with expected evidence text, answer keywords, and out-of-scope questions "
        "that must be refused. Hedged answers count as failures.</li></ul>"
        "<h3>Known gaps</h3><ul>" + "".join(f"<li>{_e(g)}</li>" for g in gaps) + "</ul>"
        "<h3>Reproduce</h3><ul>"
        "<li>Batch: <code>docker compose run --rm dashboard python batch.py &lt;same file&gt;</code> (match the sha256 above)</li>"
        "<li>RAG eval: <code>MODEL_BACKEND=&lt;backend&gt; .venv/bin/python scripts/eval_rag.py</code></li>"
        "<li>Detector: <code>.venv/bin/python scripts/generate_dataset.py &amp;&amp; .venv/bin/python scripts/eval_detector.py</code></li>"
        "<li>Canary audit: <code>.venv/bin/python scripts/canary_audit.py --ollama-log ollama.log</code></li></ul>"
    )


def _tiles(batch, rag, det, can, airgap=None) -> str:
    tiles = []
    headline = headline_latency(batch, can)
    if headline:
        latency, label = headline
        stages = latency["stages"]
        if "pii_scan" in stages:
            tiles.append((f"{_ms(stages['pii_scan']['p95'])} ms", f"PII scan P95 ({label})"))
        if "total" in stages:
            tiles.append((f"{_ms(stages['total']['p95'])} ms", f"End-to-end P95 ({label})"))
    if det:
        tiles.append((_pct(det["overall"]["recall"]), f"PII detector recall (precision {_pct(det['overall']['precision'])})"))
    if can:
        planted = can["planted"]["total"]
        tiles.append((f"{can['unmasked']['total']}/{planted}", "Canaries unmasked by the Trust Engine (sent to the models)"))
        tiles.append((f"{can['downstream']['total']}/{planted}", "Canaries found in answers, logs, or storage"))
    if rag and rag["answers_correct"] is not None:
        tiles.append((f"{rag['answers_correct']}/{rag['answerable']}", "RAG answers correct"))
    if airgap:
        tiles.append((airgap["verdict"], f"Air-gap check ({_backend_label(airgap['model_backend'])})"))
    return "<div class='tiles'>" + "".join(
        f"<div class='tile'><div class='v'>{_e(v)}</div><div class='l'>{_e(label)}</div></div>" for v, label in tiles
    ) + "</div>"


_STATUS = {"pass": "✅ Pass", "known gap": "⚠️ Known gap", "fail": "❌ Fail", "no data": "⏳ No data"}


def _criteria_section(results) -> str:
    """Acceptance criteria table; `results` are criteria.Result objects (duck-typed to avoid an import cycle)."""
    if not results:
        return ""
    rows = "".join(
        f"<tr><td>{_STATUS[r.status]}</td><td>{_e(r.criterion.label)}</td><td class='num'>{_e(r.value_text)}</td>"
        f"<td class='num'>{_e(r.target_text)}</td><td class='muted'>{_e(r.measurement.source if r.measurement else 'no selected report')}"
        f"{'<br>Known gap: ' + _e(r.note) if r.note else ''}</td></tr>"
        for r in results
    )
    return (
        "<h2>Acceptance criteria</h2><p class='muted'>Targets from <code>dashboard/acceptance.toml</code>. A miss is a "
        "known gap only when a documented cause is named; otherwise it is a fail.</p>"
        "<div class='wide'><table><tr><th>Status</th><th>Criterion</th><th class='num'>Result</th>"
        f"<th class='num'>Target</th><th>Measured on</th></tr>{rows}</table></div>"
    )


def build_report(
    batch: dict | None,
    rag: dict | None,
    det: dict | None,
    can: dict | None,
    generated_at: datetime | None = None,
    airgap: dict | None = None,
    criteria_results: list | None = None,
    prepared_for: str = "",
) -> str:
    """Return the full HTML report. Inputs are a saved batch run and summaries from `reports`."""
    generated = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>AI Sandbox Evaluation Report</title><style>{CSS}</style></head><body><main>"
        "<h1>AI Evaluation Sandbox: Evaluation Report</h1>"
        + (f"<p><b>Prepared for {_e(prepared_for)}</b> · proof-of-concept evaluation, synthetic data only</p>"
           if prepared_for else "")
        + f"<p class='muted'>Generated {generated}. Every number comes from a saved run named in its section. "
        "Evaluation data is synthetic; this report contains no question text.</p>"
        + _tiles(batch, rag, det, can, airgap)
        + _criteria_section(criteria_results)
        + _batch_section(batch)
        + _rag_section(rag)
        + _detector_section(det)
        + _canary_section(can)
        + _methodology(batch, rag, det, can, airgap)
        + "</main></body></html>"
    )
