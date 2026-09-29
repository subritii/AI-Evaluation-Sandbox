"""Attach citations to an answer in code, from the chunks that were actually retrieved.

The model is not asked to cite. When it was, it copied the prompt's template
("[source#chunk]"), invented chunk numbers, or cited nothing (see
docs/build-log.md). Instead, after generation, each retrieved chunk is scored
by how much of the answer's distinctive vocabulary it contains:

    support(chunk) = sum of idf(term) for answer terms that appear in the chunk
    idf(term)      = ln(N / chunks containing term), over the N retrieved chunks

A term found in every retrieved chunk ("policy", "customer") weighs 0; a term
found in only one ("CVV", "$250,000") weighs the most. We cite the strongest
chunk plus any within CITE_RELATIVE_CUTOFF of it. A refusal cites nothing.

By construction a citation can only point at a chunk that was retrieved and
shown to the model. Whether it's the *right* chunk is what the eval measures.
"""

import math
import re
from dataclasses import dataclass

from spacy.lang.en.stop_words import STOP_WORDS

from app.rag.retrieval import RetrievedChunk

# Cite every chunk whose support is at least this share of the best chunk's.
CITE_RELATIVE_CUTOFF = 0.6

# Phrases that mean the model declined to answer from the excerpts. A
# heuristic; the eval saves full answers so verdicts can be checked by hand.
REFUSAL_MARKERS = (
    "don't know", "do not know", "not contain", "does not mention", "do not mention",
    "not mentioned", "no information", "not address", "not covered", "not specified",
    "not provide", "unable to find", "cannot find", "can't find", "no mention",
)

_PLACEHOLDER = re.compile(r"\[[A-Z_]+_\d+\]")
# Numbers (with optional $ and thousands separators) or words of 4+ letters.
_TERM = re.compile(r"\$?\d[\d,]*(?:\.\d+)?|[a-z]{4,}")


@dataclass(frozen=True)
class Citation:
    source: str
    chunk_index: int
    section: str | None
    similarity: float  # retrieval similarity of the chunk to the question
    support: float  # how strongly the answer's terms point at this chunk


def is_refusal(answer: str) -> bool:
    """True if the answer declines to answer ("I don't know", "not mentioned", ...)."""
    normalized = answer.lower().replace("’", "'")
    return any(marker in normalized for marker in REFUSAL_MARKERS)


def terms(text: str) -> set[str]:
    """Distinctive terms: numbers (normalized, "$250,000" -> "250000") and non-stop-words."""
    text = _PLACEHOLDER.sub(" ", text).lower()
    found = set()
    for token in _TERM.findall(text):
        if token[0].isdigit() or token[0] == "$":
            found.add(token.lstrip("$").replace(",", ""))
        elif token not in STOP_WORDS:
            found.add(token)
    return found


def select_citations(answer: str, chunks: list[RetrievedChunk], refused: bool) -> list[Citation]:
    """Choose which retrieved chunks the answer is grounded in, strongest first."""
    if refused or not chunks:
        return []
    answer_terms = terms(answer)
    chunk_terms = [terms(c.content) for c in chunks]
    doc_freq = {t: sum(t in ct for ct in chunk_terms) for t in answer_terms}
    idf = {t: math.log(len(chunks) / df) for t, df in doc_freq.items() if df > 0}

    supports = [sum(idf[t] for t in answer_terms & ct if t in idf) for ct in chunk_terms]
    best = max(supports)
    if best <= 0:
        return []
    cited = [
        Citation(c.source, c.chunk_index, c.section, round(c.similarity, 4), round(s, 3))
        for c, s in zip(chunks, supports)
        if s >= CITE_RELATIVE_CUTOFF * best
    ]
    return sorted(cited, key=lambda c: -c.support)
