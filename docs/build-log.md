# Build Log

What broke, what we changed, and what's still known to be weak. Every number
here comes from a real run and names the script and report that produced it.

---

## Task 1: Local infrastructure + RAG

### Baseline (commit `23ba6b1`)

`.venv/bin/python scripts/eval_rag.py` → `reports/eval_rag_20260929-153914.json`
(llama3.2:3b, nomic-embed-text, top_k=4, chunk_size=1000, temperature 0, 19 chunks, all-Docker Ollama on CPU)

| Metric | Result |
|---|---|
| Expected chunk retrieved in top 4 | 9/9 (8 at rank 1) |
| Correct answers (keywords present, no hedging) | 7/9 |
| Correct refusals (out-of-scope questions) | 2/2 |
| Total time per question | 6.8 s to 56.2 s, mostly generation |

A second run from the same commit produced identical answers for all 11 questions.

### Fixes along the way

- **Chunks straddled sections.** Size-only splitting put the end of section 8
  (cash transactions) and the start of section 9 (Payment Card Data) in one
  chunk, so the CVV rule shared an embedding with unrelated text. Fix: split
  Markdown on headers first, fall back to size for long sections, and prefix
  each chunk with its heading. The policy went from 14 to 19 chunks, and the CVV
  chunk became the top result for CVV questions.
- **The first scorer passed contradictory answers.** "No, we don't know. The
  policy states CVVs must never be stored..." contained the keywords and
  passed. Reading the saved answers caught it. Now an answer that refuses or
  hedges on a question the policy covers fails.
- **Scores changed between identical runs.** At Ollama's default temperature
  the CVV answer was clean in one run and hedged in the next. Fix:
  `LLM_TEMPERATURE=0` (greedy decoding), confirmed by the identical re-run above.

### Known limitations

1. **The model quotes rules but won't apply them.** Retrieval is right in both
   failures; generation is the problem.
   - *"When must a CVV be deleted?"* The model quotes "must never be stored
     after authorization" and then says it doesn't know, because the policy
     never uses the word "delete".
   - *"What approval is needed for a $300,000 wire transfer?"* The model quotes
     "wire transfers of $250,000 or more require approval by two authorized
     officers" and still says the policy doesn't cover $300,000. It doesn't
     make the threshold comparison.

   Both look like reasoning limits of a 3B model combined with a strict "say
   you don't know" system prompt. Options to evaluate: a larger local model,
   a prompt that allows applying a quoted rule, or both, measured against
   this baseline.
2. **Model-written citations are unreliable.** The prompt asks for
   `[source#chunk]`. Answers cite the literal template (`[source#chunk]`),
   invent chunk numbers (`[source#18]` for the CVV rule in chunk 11), use a
   heading instead (`[8. Payments and Wire Transfer Controls#10]`), or cite
   nothing. The eval doesn't score citations yet. Planned fix: citations
   attached by code from the retrieved chunks, plus a citation-accuracy check.
3. **Keyword and refusal scoring is heuristic.** The refusal check looks for
   phrases like "don't know". Full answers are saved in each report so every
   verdict can be checked by hand.
4. **Small eval set.** 11 questions, written by the same person who wrote the
   policy. Good for catching regressions, not a statistically meaningful
   accuracy estimate.

---

## Task 2: Trust Engine (+ lean Task 3 detector evaluation)

### Detector accuracy (commit `ee46796`)

`.venv/bin/python scripts/generate_dataset.py && .venv/bin/python scripts/eval_detector.py`
→ `reports/eval_detector_20260929-160935.json`

Scrubber `presidio-2.2.364+en_core_web_lg-3.8.0+rules-v1`, threshold 0.4. Dataset: 1,000 synthetic
records (seed 42, sha256 `50e7fbd690777a54…`), 1,802 labeled spans.
A gold span counts as found when a prediction of the **same type** overlaps it.

| Entity | Support | Precision | Recall | F1 | TP | FP | FN | Exact span |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| PERSON | 595 | 99.5% | 92.8% | 96.0% | 552 | 3 | 43 | 537/552 |
| EMAIL_ADDRESS | 142 | 100.0% | 100.0% | 100.0% | 142 | 0 | 0 | 142/142 |
| PHONE_NUMBER | 290 | 100.0% | 88.6% | 94.0% | 257 | 0 | 33 | 257/257 |
| US_SSN | 133 | 100.0% | 100.0% | 100.0% | 133 | 0 | 0 | 133/133 |
| CREDIT_CARD | 89 | 100.0% | 79.8% | 88.8% | 71 | 0 | 18 | 71/71 |
| IBAN_CODE | 87 | 100.0% | 100.0% | 100.0% | 87 | 0 | 0 | 87/87 |
| US_ROUTING_NUMBER | 221 | 99.4% | 78.3% | 87.6% | 173 | 1 | 48 | 173/173 |
| ACCOUNT_NUMBER | 245 | 100.0% | 78.0% | 87.6% | 191 | 0 | 54 | 191/191 |
| **Overall (micro)** | 1802 | 99.8% | 89.1% | 94.1% | 1606 | 4 | 196 | 1591/1606 |

| Slice | Precision | Recall | Spans |
|---|---:|---:|---:|
| Standard templates (clear context, common formats) | 99.9% | 96.2% | 1,363 |
| Hard templates (no context, abbreviations, bare digits) | 100.0% | 67.2% | 439 |
| No-PII records with any detection | | 3 of 150 | |

`pii_scan` latency: mean 5.85 ms, P50 5.04 ms, P95 9.4 ms per record (host
`.venv`, CPU; spaCy model load of 1.7 s excluded). The container runs the same
pinned versions, but these timings were measured on the host.

**How to read this.** I wrote both the recognizers and the dataset templates,
so these numbers are optimistic. They show the detector does what it was
designed to do and catch regressions; they don't estimate accuracy on real bank
text. To limit self-grading, the dataset was written before the first eval run,
and no recognizer was tuned against its results. The one code change after the
first run was a bug fix (below), and both runs are recorded. For privacy,
**recall is the number that matters**: a miss leaks, a false positive only
over-masks.

### Where recall is lost (not tuned away)

- **No context words (0% on `no_context_numbers`).** "Send the funds to
  080154303 / 38299737631" has nothing saying what those numbers are. This is
  deliberate: flagging every bare 6-17 digit number would mask order IDs, ticket
  numbers, and ZIP+4s everywhere.
- **Bare phone after "call" (0% on `bare_phone_call`).** "call" is a spaCy stop
  word, and Presidio builds the context window from non-stop-words, so it can
  never act as context. The same bare digits after "phone" are caught (100%).
- **Abbreviations Presidio doesn't tokenize as words.** "A/C" for account
  (`rtn_abbrev` 50%: RTN is caught, A/C isn't) and "bank code" for routing
  (`bank_code` 45.8%).
- **Card formats outside Presidio's regex:** 12-digit Maestro, 19-digit Visa,
  2-series Mastercard (2221-2720), 15-digit JCB. Luhn-valid, so real card numbers.
- **Non-English names.** en_core_web_lg misses some non-English
  names ("Ingo Rohleder", "Débora Sarabia").
- **Context windows are 5 words wide.** In "routing number 058969106, account
  number 713611155", the account number is also within reach of "routing" and is
  labeled a routing number (still masked, wrong type).
- **PERSON false positives on IDs:** "CHB-POL-350" and a ticket number were
  tagged as people (3 of 150 negatives).

### Debugging stories

- **Presidio's built-in routing recognizer ignores context.** A passing ABA
  checksum sets the score to 1.0, and about 1 in 10 random 9-digit numbers
  pass. Replaced with a recognizer where the checksum can only *reject*, so
  context words decide.
- **"routing number" boosted phone numbers.** Presidio's phone recognizer lists
  "number" as a context word, so "routing number 021000021" came out as a
  PHONE_NUMBER at 0.75. It also scored any parseable digit run at exactly the
  threshold. Now bare digits need phone context.
- **The routing context never fired.** spaCy lemmatizes "routing" to "rout", and
  Presidio matches context against lemmas by substring. The context word is now
  "rout".
- **The eval found a bug in the phone fix.** python-phonenumbers returned
  `" 9826204505"` with a leading space, which slipped past the bare-digit
  check. Fixing it removed all 6 phone false positives (phone precision 97.7% →
  100.0%, overall 99.4% → 99.8%; recall unchanged). The pre-fix run is
  `reports/eval_detector_20260929-160755.json`.
- **"Email maria@..." tags "Email" as a PERSON.** A spaCy NER quirk with
  capitalized sentence-initial words. Kept as a strict `xfail` test so a fix
  shows up.
- **Two hidden network calls.** Presidio downloads a missing spaCy model at
  runtime, and its email recognizer downloads the public-suffix list on first
  use. Now the model is installed at image build time (and startup fails if it's
  missing), and email validation uses tldextract's bundled snapshot. A test blocks
  sockets during scrubbing.

### Design decisions

- **Explicit recognizer registry and entity list.** Only the eight PII types are
  masked. Presidio's NER also emits DATE_TIME, LOCATION, and ORG; masking those
  would wreck policy text ("within 30 days"). The mock policy scrubs to zero
  detections, which a test enforces.
- **Own masking instead of presidio-anonymizer.** The anonymizer's operators are
  set per entity type, so it can't number placeholders by appearance or reuse a
  number for a repeated value. The replacement is about 20 lines and tested.
  Overlaps are resolved across types (Presidio only merges same-type overlaps)
  by score, then length.
- **`scrubbed_by` names the exact scrubber**
  (`presidio-2.2.364+en_core_web_lg-3.8.0+rules-v1`). After re-ingest, all 19
  rows carry it and none say `passthrough-stub`.

---

## Task 4 + Task 6: Gateway latency and canary audit (native-Ollama configuration)

**Configuration for every number in this section: native Ollama 0.34.4 on the
Mac (Apple GPU), llama3.2:3b + nomic-embed-text, temperature 0, top_k=4, 19
chunks re-ingested through native Ollama.** This is the dev option, not the
all-Docker demo setup. Docker Ollama on macOS runs on CPU, so these latencies do
**not** describe the all-Docker configuration. Both reports and every
`latency_samples` row record `model_backend=native` and the model names (commit
`6e7b5cb`). Host load average (1 min) was 5-11 on 8 CPUs during the runs, since
other work was running on the machine; each report records it.

### RAG eval baseline (commit `6e7b5cb`)

`MODEL_BACKEND=native .venv/bin/python scripts/eval_rag.py` →
`reports/eval_rag_20260929-195959.json`

| Metric | Result |
|---|---|
| Expected chunk retrieved in top 4 | 9/9 (8 at rank 1) |
| Correct answers (keywords present, no hedging) | 9/9 |
| Correct refusals | 2/2 |
| Evidence chunk cited (code-attached citations) | 9/9, mean citation precision 100% |
| Refusals citing nothing | 2/2 |
| Total time per question | 1.8 s to 8.3 s |

The Task 1 baseline was 7/9. Two things changed since then: the system prompt
was rewritten in `b85875d`, and the runtime moved from Docker to native Ollama.
The gain can't be credited to either one without a Docker run at this commit.
With 11 questions, the per-stage percentiles here aren't meaningful. The first
question's `pii_scan` (3,035 ms) also includes loading the Trust Engine,
because `eval_rag.py` has no warmup, unlike the gateway.

### Per-stage latency, 200-request canary batch

`.venv/bin/python scripts/canary_audit.py` (200 requests, 3 warmups excluded,
seed 2026) → `reports/canary_audit_20260929-200203.json`, run
`canary-20260929-200203`. All 200 returned 200 OK in 6.8 min.

| Stage (ms) | avg | P50 | P90 | P95 | P99 |
|---|---|---|---|---|---|
| **pii_scan** (Trust Engine) | 53 | 35 | 73 | **126** | 307 |
| retrieval (embed + pgvector) | 64 | 55 | 108 | 136 | 185 |
| time_to_first_token | 627 | 293 | 1,536 | 1,640 | 2,409 |
| generation | 1,224 | 1,220 | 2,019 | 2,234 | 2,505 |
| **total** | 1,970 | 1,734 | 3,259 | **3,590** | 3,991 |

The PII scan is 3.5% of end-to-end time at P95 (126 of 3,590 ms). At
n=200, each P99 rests on about 2 requests, so treat it as indicative.

### Canary leakage: 50/207 planted canaries found, all in `masked_question`

207 synthetic canaries in 106 of the 200 requests. The canaries were found
**only** in `masked_question`, the text sent to the embedding model and the LLM.
That means the Trust Engine failed to mask them. Nothing was found in answers,
other response fields, backend or db container logs, or any text column in the
database.

| Entity | Planted | Found | Where the misses come from |
|---|---|---|---|
| US_ROUTING_NUMBER | 37 | 17 | all 17 from `no_context_wire` (bare digits); 0/20 with context |
| ACCOUNT_NUMBER | 37 | 17 | all 17 from `no_context_wire`; 0/20 with context |
| PERSON | 51 | 9 | 5 in `no_context_wire`; 4 others, mostly non-English names |
| CREDIT_CARD | 18 | 7 | 12-digit Maestro, 19-digit Visa, 2-series Mastercard, 15-digit JCB |
| US_SSN | 21 | 0 | |
| IBAN_CODE | 17 | 0 | |
| EMAIL_ADDRESS | 13 | 0 | |
| PHONE_NUMBER | 13 | 0 | |

Every miss matches a limitation listed under Task 2, "Where recall is lost."
Leaving out the deliberately hard `no_context_wire` template, 11 of 156
canaries were found (7%).

**Not searched:** the native Ollama server log. `ollama serve` was writing to a
terminal, not a file, and the stopped Ollama container's log says nothing about
native Ollama. The report lists this under `not_searched`. To close the gap,
run `ollama serve > ollama.log 2>&1` and pass `--ollama-log ollama.log`.

A 20-request smoke test ran first (`reports/canary_audit_20260929-195550.json`,
6/21 found, same pattern). Its latency rows predate the model-config columns
and are stored as `unknown`.

### Fix: four card formats Presidio's pattern missed (commit `9e69f46`)

Presidio's card regex only accepts 13-17 digits starting with 1, 3, 4, 50-55,
or 6. The 7 cards leaked in the canary audit, and the 18 card misses in the
detector eval, were 4 Luhn-valid formats outside that pattern: 12-digit Maestro
(4 of the 7), 19-digit Visa (1), 2-series Mastercard 2221-2720 (1), and 15-digit
JCB 2131 (1). `BankCardRecognizer` now handles them in two tiers:

- **13-17 digits:** Luhn alone flags the number, as before, now with the
  2221-2720 and 2131 prefixes added.
- **12, 18, 19 digits:** Luhn **plus** a card context word ("card", "debit",
  "visa", ...). About 1 in 10 random numbers pass Luhn, and bare 12-digit
  strings are common (account numbers, order IDs), so Luhn alone would
  over-mask. Unit tests cover the negatives: no context, failed Luhn, and
  account context.

Rules version bumped to `v2`, and the policy chunks were re-ingested (19 rows,
`...+rules-v2`, 0 masked entities).

**Caveat:** this change was driven by misses in this same detector dataset
and canary set, unlike the Task 2 recognizers. The "after" recall is therefore
optimistic for these formats. All dataset card cases have a card word nearby,
so the eval can't show the context-required tier's behavior on bare numbers;
the unit tests cover that.

**Detector eval** (same dataset, sha256 `50e7fbd690777a54…`):
before `reports/eval_detector_20260929-203456.json` (rules-v1, reproduces the
Task 2 table exactly) → after `reports/eval_detector_20260929-203620.json`
(rules-v2).

| Metric | Before (v1) | After (v2) |
|---|---:|---:|
| CREDIT_CARD recall | 79.8% (71/89) | **100.0% (89/89)** |
| CREDIT_CARD precision | 100.0% | 100.0% |
| Overall recall (micro) | 89.1% (1606/1802) | **90.1% (1624/1802)** |
| Overall precision | 99.8% (4 FP) | 99.8% (4 FP) |
| Standard templates recall | 96.2% | 97.5% |
| Hard templates recall | 67.2% | 67.2% |
| No-PII records with a detection | 3 of 150 | 3 of 150 |
| `pii_scan` per record, P95 (host) | 5.65 ms | 6.2 ms |

All other entity rows are unchanged.

**Canary audit, native Ollama** (same seed 2026, 200 requests, 207 canaries):
before `reports/canary_audit_20260929-200203.json` → after
`reports/canary_audit_20260929-203656.json` (run `canary-20260929-203656`,
gateway reported `backend=native`, llama3.2:3b, nomic-embed-text).

| Entity | Planted | Found before | Found after |
|---|---:|---:|---:|
| CREDIT_CARD | 18 | 7 | **0** |
| US_ROUTING_NUMBER | 37 | 17 | 17 |
| ACCOUNT_NUMBER | 37 | 17 | 17 |
| PERSON | 51 | 9 | 9 |
| SSN, IBAN, email, phone | 64 | 0 | 0 |
| **Total** | 207 | **50** | **43** |

The remaining 43 are all `no_context_wire` digits (34) and missed names (9),
unchanged. All 43 were found only in `masked_question`.

**The native Ollama log was searched this time.** `ollama serve` was
restarted with its output going to `ollama.log`, and the audit read the part
written during the run (about 1 MB, 203 chat and 204 embed requests). It
contained 0 canaries. The 43 unmasked values *were* sent to Ollama, so this
also shows that Ollama 0.34.4 at default verbosity doesn't write prompt text
to its log. Answers, backend and db logs, and every database text column
also contained 0.

Latency in the rerun (ms, n=200):

| Stage | P50 | P90 | P95 | P99 |
|---|---:|---:|---:|---:|
| **pii_scan** | 34 | 62 | **92** | 203 |
| retrieval | 46 | 73 | 84 | 149 |
| time_to_first_token | 192 | 1,565 | 1,752 | 2,188 |
| generation | 1,186 | 1,982 | 2,134 | 2,241 |
| **total** | 1,600 | 3,098 | **3,341** | 3,647 |

This run is faster than the first (pii_scan P95 126 → 92 ms, total P95 3,590 →
3,341 ms), but host load average was 2.2-4.1 instead of 5.3-7.7. The difference
is mostly machine load, not the recognizer change: the detector eval, which
measures pii_scan in isolation, shows no speedup.

---

## Task 7: Dashboard + downloadable report

### What

A Streamlit dashboard in its own container (`dashboard`, http://localhost:8501)
that goes from upload to report without the terminal:

1. **Run a batch.** Upload a CSV (`question` column) or JSONL
   (`{"question": ...}` per line), up to 1,000 questions. The questions go
   through the gateway one by one. A live chart (log scale, so the ~50 ms PII
   scan and the ~seconds of generation fit on one chart) updates after every
   request. Then the gateway's per-stage P50/P90/P95/P99 table appears.
2. **Results.** The saved RAG eval, detector accuracy, and canary audit reports,
   newest by default and selectable in the sidebar. Each shows its source file,
   start time, commit, and model backend.
3. **Report.** One self-contained HTML file: summary tiles, batch latency,
   RAG quality, detector accuracy, canary audit, and a methodology section.
   The methodology covers pipeline, configuration as reported by the gateway,
   how latency is measured, machine load per run, known gaps, and commands to
   reproduce each number.

Verified in Chrome: uploaded `data/sample_batches/policy_questions.csv`, ran it
(20/20 OK, run `dashboard-20260929-210005`, native Ollama), watched the chart
update, opened Results and the report preview. I didn't click the final
Download button during that test, because downloading needs the user's OK.
Instead, the report built from the same selected runs was checked: 17 KB, no
`http(s)://`, no `<script>`, and none of the sample batch's questions or
synthetic PII.

### How

- `dashboard/batch.py`: parses uploads (errors name the row, never its
  content), sends each question to `POST /query` under one `run_id` (warmups
  use a separate `<run_id>-warmup` id and a fixed PII-free question), then
  fetches `GET /metrics/{run_id}` and saves `reports/dashboard_batch_<ts>.json`.
  It's also a CLI:
  `docker compose run --rm dashboard python batch.py /data/sample_batches/policy_questions.jsonl`.
- `dashboard/reports.py`: reduces each saved report type to a summary with
  provenance. Fields older reports lack come back as `None` and display as
  "not recorded".
- `dashboard/report_html.py`: builds the report with inline CSS only, escapes
  every value, and has a dark-mode palette.
- Gateway: new `GET /info` returns non-secret configuration (backend, models,
  temperature, max tokens, scrubber version) and the load average where the
  gateway runs.
- Tests: 22 dashboard tests (`docker compose run --rm --no-deps dashboard
  pytest`) with HTTP faked via `httpx.MockTransport`. They cover parsing and
  error messages, no question text in saved runs, the report's sections,
  provenance, backend-mismatch warning, missing-data wording, and
  self-containment. Plus one gateway test for `/info`; backend suite at 67
  passed + 1 xfail.

### Why

- **The dashboard only talks to the gateway.** It has no database credentials,
  so it can only do what the API allows, and it measures the same path a real
  client uses. The alternative, querying Postgres directly, would be quicker to
  build but would put credentials in a second container and create a second
  path to the data.
- **Percentiles come from the gateway, not the dashboard.** `/metrics/{run_id}`
  runs the same numpy code as every script, so a dashboard number and a
  script number can't disagree. The live chart plots raw per-request timings
  only.
- **Saved runs and the report contain no question text, not even masked
  text.** The first design stored masked questions. That's wrong: the canary
  audit shows the Trust Engine misses some PII (43/207), so "masked" text can
  still hold it, and reports get emailed. Masked questions appear only on
  screen, in-session, for the person who uploaded them. Runs are tied to their
  input by the file's sha256 instead.
- **The canary audit stays a host script.** It reads `docker compose logs`,
  which would need the Docker socket in the dashboard container. The socket
  effectively grants root on the host, which is too much for a UI. The
  dashboard shows its saved reports instead.
- **Every section names its run, and the report says when runs don't match.**
  If the RAG eval ran on a different backend than the batch, the report warns
  about it. Missing reports are stated as missing, never filled in. That's
  rule 4 applied to presentation.
- **Machine load is labeled by where it was measured.** The batch's load
  comes from the gateway's environment, which is the Docker Desktop VM on
  macOS. With native Ollama the models run on the Mac, outside the VM, so the
  report says the VM figure excludes model load. In the verification run the
  VM showed 0.5 while the host scripts had recorded 2-11 on the Mac.
- **Two Streamlit network calls turned off (rule 3).** `gatherUsageStats =
  false` stops usage telemetry. Separately, bound to `0.0.0.0` with no server
  address, Streamlit calls `checkip.amazonaws.com` at startup to print an
  "External URL". The first container start did exactly that; the log showed
  the machine's public IP. `browser.serverAddress = "localhost"` skips the
  lookup, and the log now prints only `URL: http://localhost:8501`. The other
  outbound call sites in Streamlit (remote scripts, remote theme files, the
  email prompt) don't run in this setup. Task 8's internal network will
  enforce this instead of relying on config.

### Known limitations

- Requests are sequential by design, so a 1,000-question batch on Docker
  Ollama (CPU) takes hours. There's no cancel button; stopping the browser tab
  doesn't stop a running batch.
- Streamlit keeps imported modules cached, so edits to `batch.py`,
  `reports.py`, or `report_html.py` need a container restart (`app.py` reloads
  on refresh).
- The report's "Known gaps" list is written by hand from this log. The canary
  "not searched" items are the only gaps pulled from run data.

### Report fixes after review (commit `1f9e6cc`)

- **Canary results split in two.** "Found" mixed two different outcomes. Now
  the tile and section report **unmasked by the Trust Engine** (the value was
  in the masked question, so the models received it: 43/207 in the native
  200-request run) separately from **found in answers, logs, or storage**
  (0/207). Both count distinct canaries, taken from the report's findings.
- **Headline latency uses the largest run.** The tiles had shown the selected
  dashboard batch (n=20, so P95 rests on one request). They now use whichever
  selected run has the most samples, and each tile says n, the run, and the
  backend. The canary section got its own latency table so the tile's
  source is in the report.
- **"Nothing leaves the machine" was an overclaim** before Task 8. It now says
  no external services are called, and that network isolation is enforced
  only in the all-Docker configuration. After Task 8 the statement cites the
  air-gap check report (below).
- **Detector eval rerun from a clean commit.** The before/after runs above
  cite `17c3c66-dirty`. The rerun at `1f9e6cc` (clean) →
  `reports/eval_detector_20260929-212122.json` gives identical numbers
  (recall 90.1%, precision 99.8%, cards 89/89).

---

## Task 8: Packaging + air-gap proof

### What

- **One-command start.** After the one-time `./scripts/pull_models.sh`,
  `docker compose up -d` brings up db and ollama, runs a one-shot `ingest`,
  then starts backend, dashboard, and the proxy (87 s on the first start
  here).
- **Isolated network.** db, ollama, ingest, backend, dashboard, and tools run
  on `sandbox`, an `internal: true` network. A read-only, unprivileged nginx
  `proxy` joins `sandbox` and `edge` and publishes only
  `127.0.0.1:8501` (dashboard) and `127.0.0.1:8000` (API). The DB and Ollama
  have no host ports.
- **`scripts/airgap_check.py`** proves it and saves
  `reports/airgap_check_<ts>.json`, which the dashboard and HTML report cite.

**Results, all-Docker (commit `2ef114f` unless noted):**

| Check | Result |
|---|---|
| Air-gap check | **PASS**, `reports/airgap_check_20260929-213132.json`; again at `23579ac` → `airgap_check_20260929-214335.json` |
| Egress from backend, dashboard, and the sandbox network | TCP to 1.1.1.1:443 and 8.8.8.8:53: unreachable. `example.com` and `registry.ollama.ai`: no DNS |
| Control probe on Docker's default bridge | all four connected / resolved |
| Internal reachability | db, ollama, backend, dashboard all reachable |
| Ingress via proxy | dashboard and API HTTP 200 |
| Proxy's own egress | CONNECTED (reported in the check; see Why) |
| Real query through the isolated stack | HTTP 200, cited the policy |
| **Negative test:** native-Ollama override | **FAIL** as expected, `airgap_check_20260929-214307.json`: backend dual-homed and reached all four targets |
| RAG eval in `tools` (Docker Ollama on CPU) | 9/9 answers, 2/2 refusals, evidence cited 9/9, 100% precision; 7.4-45.3 s per question. `reports/eval_rag_20260929-213200.json` |
| Canary audit, 20 requests | 6/21 unmasked (same misses as the native smoke test), **0/21 in answers, logs (including the Ollama container log), or storage**. `reports/canary_audit_20260929-213533.json` |
| Canary latency, all-Docker, n=20 | PII scan P50/P95 100/364 ms; end-to-end P50/P95 15.1/26.7 s. Host load average 28.6 → 17.4 on 8 CPUs, so these are CPU-contended numbers |

Same Ollama version in both configurations (0.34.4), so native vs all-Docker
differs in runtime and hardware, not model server version. A 200-request
all-Docker canary run would take about 2 hours on CPU and hasn't been run.

### How

- **Compose:** networks `sandbox` (internal), `edge` (proxy only), and `setup`
  (model pull only). A shared env anchor for backend, ingest, and tools.
  Ollama and nginx are pinned by digest to the tested images.
- **Model pull:** the running `ollama` container can't download anything, so
  `pull_models.sh` runs `ollama-pull` (profile `setup`), a one-off container
  on the `setup` network that shares the model volume and exits.
- **Evaluation scripts:** a `tools` build stage (backend image plus git, so
  reports still record their commit) mounts the repo read-only and `reports/`
  read-write, on `sandbox`. The canary audit stays on the host: API through
  the proxy, logs via `docker compose logs`, and the DB via `docker compose
  exec db psql`, so it needs no ports.
- **Hardening:** backend, tools, and dashboard images run as uid 10001. The
  proxy runs as nginx's unprivileged user with a read-only root filesystem,
  a tmpfs `/tmp`, all capabilities dropped, and `no-new-privileges`. Its
  access log records method, path, and status, never bodies.
- **Native override:** `ollama` is moved to a profile so it isn't started, and
  backend, ingest, and tools also join `edge` to reach
  `host.docker.internal`. The file header says it is not isolated.

### Why

- **Tested the Docker behavior before designing around it.** Two throwaway
  probes decided the layout:
  1. On Docker Desktop, a container on an internal network **cannot publish
     ports** (host connection failed) and has no egress (no route to
     1.1.1.1, no DNS). So an ingress proxy is required.
  2. I then tried to remove the proxy's route out by disabling outbound NAT on
     its bridge (`enable_ip_masquerade=false`). Ingress still worked, but
     **egress also still worked**, DNS included: Docker Desktop's VM
     networking NATs regardless of the flag. Using that flag without testing
     would have produced a false isolation claim. So the proxy keeps a route
     out, and the air-gap check reports it instead of hiding it.
- **Why a proxy rather than dual-homing the dashboard and backend.** Putting
  the app containers on a normal network to publish ports would give the
  containers that hold data a route out. The proxy is the smallest possible
  dual-homed piece: no data, no code, a static config.
- **Why the control probe.** Without it, "all probes blocked" is also what you
  would see with the Wi-Fi off. The check returns INCONCLUSIVE unless the
  same probe connects from a normal network at the same moment.
- **Why a negative test.** A check that has never failed proves little.
  Running it against the native-Ollama configuration, which really does have
  a route out, produced FAIL with the exact cause.
- **Why a `tools` container instead of publishing DB/Ollama ports.** Publishing
  them would need a non-internal network and reopen a route out. Running the
  scripts inside the sandbox measures the same pipeline without weakening it.
- **Why ingest on every start.** It replaces each source's chunks, so it's
  idempotent. It also guarantees stored and query embeddings come from the
  same runtime after switching between native and Docker Ollama, which
  previously had to be remembered by hand.

### Known limitations

- The proxy has a route to the internet. It holds no data, but a compromised
  proxy could reach out. Docker Desktop offers no way to publish a port
  without that; on Linux, host firewall rules (DOCKER-USER chain) could drop
  its egress.
- The air-gap check is point-in-time: it proves the state of the running
  stack when it ran. Rerun it after any compose change.
- Setup (`pull_models.sh`, image builds) needs the internet; the isolation
  covers runtime only.
- Source directories are still bind-mounted into backend, ingest, and
  dashboard for development, so the running code is the working tree, not
  only the image.
- Mistake during the proof, recorded for honesty: the first proof run started
  before the Task 8 code was committed, so its air-gap report
  (`airgap_check_20260929-212849.json`) cites `1f9e6cc-dirty`. I stopped the
  eval, committed, and reran everything from `2ef114f`; those are the reports
  cited above.

---

## Maintenance (2026-09-30)

- **Pruned Docker build cache.** `docker builder prune -f` freed 1.7 GB
  inside Docker. macOS free space didn't change right away: `Docker.raw` stayed
  at 15 GB, because Docker Desktop's disk image doesn't shrink as soon as space
  is freed inside it. Recorded in the runbook.
- **Dashboard default report.** Canary audits now default to the run with
  the most latency samples (the 200-request native run) rather than the newest
  (a 20-request all-Docker run), because they feed the headline latency tiles.
  Other report kinds stay newest-first (commit `e7f7214`).

---

## Task 11: Runbook

`docs/runbook.md` (commit `cf01c89`): measured hardware needs, install, daily
operations, the all-Docker vs native configurations (including the
`COMPOSE_FILE` trap), a reference for every setting with its precedence, and
fixes for the problems hit while building this. Where an entry describes
what happened here, it says so. Entries not observed directly (a corrupted
Docker disk, Docker Desktop not launching) give the standard recovery steps
without inventing details, and destructive steps are marked.

Checking the working `.env` against `.env.example` (key names only) found
real drift: `MODEL_BACKEND`, `LLM_TEMPERATURE`, `LLM_MAX_TOKENS`, and both
proxy port keys were missing (code defaults applied, and host-run scripts
recorded `model_backend=unknown` unless it was typed inline), while two
obsolete port keys remained. The `.env` wasn't edited; the runbook gives the
check.

---

## Task 9: Ollama or any OpenAI-compatible endpoint

### What

`LLM_PROVIDER` and `EMBED_PROVIDER` (`ollama` | `openai_compatible`) choose
the API for each model; `OPENAI_BASE_URL` and `OPENAI_API_KEY` configure the
endpoint. Tested end to end by pointing both providers at Ollama's own
OpenAI-compatible API (`http://ollama:11434/v1`) inside the sandbox, all from
commit `0682c2f`, all-Docker (CPU):

| Check | `ollama` API (Task 8 runs) | `openai_compatible` API |
|---|---|---|
| Air-gap check | PASS | **PASS** (`airgap_check_20260930-160446.json`): endpoint is in-network |
| RAG retrieval, expected chunk in top 4 | 9/9 (8 at rank 1) | **9/9 (8 at rank 1)**, same ranks per question |
| Correct answers / refusals | 9/9 / 2/2 | **9/9 / 2/2** |
| Evidence cited, citation precision | 9/9, 100% | **9/9, 100%** (`eval_rag_20260930-160653.json`) |
| Canaries unmasked / found downstream (20 requests) | 6/21 / 0/21 | **6/21 / 0/21** (`canary_audit_20260930-161023.json`) |

The Trust Engine results are identical, as they should be: masking happens
before either provider is called. Latency isn't compared across these runs:
host load was 12-13 in the second run and 17-29 in the first, on CPU.

### How

- `app/rag/model_client.py`: `OpenAICompatibleClient` streams
  `/chat/completions` (server-sent events, `data: {...}` lines until
  `[DONE]`) and calls `/embeddings` (reordered by `index`). `ModelClient`
  routes `embed()` and `chat_stream()` to their providers, sharing one client
  when both are the same. The pipeline, Trust Engine, ingest, and eval call it
  exactly as they called the Ollama client.
- Errors from either provider are `ModelServerError` (the gateway still
  returns 502 and logs the type only).
- Recording: `/info`, every `latency_samples` row (new `llm_provider` and
  `embed_provider` columns), `GET /metrics`, the RAG eval config, and the HTML
  report carry provider and endpoint. `MODEL_BACKEND` gained `remote`.
- Tests: 16 new (`test_model_client.py`, via `httpx.MockTransport`): SSE
  parsing including keep-alives, empty deltas, and `[DONE]`; embedding order;
  auth header present only with a key; errors (401, 404, 500, and a
  mid-stream error) raised without the key; provider selection; config
  validation; credentials stripped from reported URLs; the key absent from
  `repr` and `/info`. Backend suite: 82 passed + 1 xfail; dashboard: 33.

### Why

- **Per-model providers.** A common real setup is keeping embeddings local
  (no documents leave) while generation moves to a bigger model. One switch
  for both would force both to move.
- **Masking still happens before any provider is called.** The provider
  sits behind the same two calls the pipeline already made after the Trust
  Engine, so "Trust Engine and audits unchanged" is a property of the design,
  not something each provider has to get right. The identical canary result
  above is the check.
- **Why "remote" is its own label and override.** Where the model runs is the
  biggest factor in both latency and data exposure. An endpoint outside the
  sandbox can't be reached without giving the backend a route out, so that
  path is an explicit Compose override, labeled `remote`, and fails the
  air-gap check. Supporting it through `.env` alone would have quietly
  weakened isolation.
- **Tested against Ollama's `/v1` instead of a cloud API.** It exercises the
  real OpenAI wire format end to end while staying inside the rules: no
  external calls, air-gap intact.
- **Existing latency rows labeled `ollama`, not `unknown`.** Before this
  change the code could only call Ollama, so that label is a fact.
- **Rule 3 in CLAUDE.md was reworded** from "Ollama only" to "a model server
  the operator controls; external endpoints only as a labeled, non-isolated
  opt-in", because the task deliberately changes what that rule allowed.

### Known limitations

- Only tested against Ollama's OpenAI-compatible API; other servers differ in
  small ways (e.g. whether `max_tokens` or `max_completion_tokens` is
  accepted, error formats). The client sends the widely supported fields.
- The nomic task prefixes are added for every embedding provider; with a
  different embedding model they're harmless text, but retrieval must be
  re-measured.
- Embeddings must stay 768-dimensional without a schema change.
- The `ollama` container starts even when neither model uses it.
