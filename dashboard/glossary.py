"""Plain-language definitions shown as tooltips (help=) wherever these terms appear."""

TERMS = {
    "p95": ("P95 (95th percentile): 95% of requests were at least this fast; the slowest 5% took longer. "
            "It reflects what users notice better than an average. With n requests it rests on the slowest n/20, "
            "so small runs give noisy P95s."),
    "p50": "P50 (median): half of the requests were faster than this, half slower.",
    "p99": "P99: 99% of requests were at least this fast. Below 100 samples it rests on one or two requests.",
    "recall": ("Recall: of the PII actually present, the share the detector found. For privacy this matters most: "
               "a miss means the value reached the models."),
    "precision": ("Precision: of everything the detector flagged, the share that really was PII. "
                  "Low precision over-masks harmless text, such as order numbers."),
    "canary": ("Canary: a unique fake PII value planted in a test request, then searched for everywhere data could "
               "land: responses, logs, and every text column in the database. Finding one where it shouldn't be is a leak."),
    "air_gap": ("Air gap: the containers that handle data have no network route to the internet. Tested by trying to "
                "connect out from inside them, while a control probe on a normal network must succeed."),
    "unmasked": ("Unmasked: the canary was still in the question after the Trust Engine, so the embedding model and "
                 "the local LLM received it. A detector miss, not a leak into storage."),
    "downstream": "In answers, logs, or storage: the canary showed up in an API answer, a searched log, or the database.",
    "similarity": "Cosine similarity between the question's embedding and the chunk's (1.0 = same direction).",
    "support": "Citation support: how strongly the answer's wording overlaps the chunk (higher = more of the answer came from it).",
}


def term(key: str) -> str:
    return TERMS[key]
