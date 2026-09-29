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
