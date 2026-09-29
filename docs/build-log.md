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
