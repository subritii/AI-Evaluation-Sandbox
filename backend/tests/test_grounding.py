"""Tests for code-attached citations and refusal detection (no model or DB needed)."""

from app.rag.grounding import is_refusal, select_citations, terms
from app.rag.retrieval import RetrievedChunk


def chunk(index: int, content: str, section: str) -> RetrievedChunk:
    return RetrievedChunk(source="policy.md", chunk_index=index, content=content, similarity=0.7, section=section)


CHUNKS = [
    chunk(10, "## 8. Wire Controls\n\nWire transfers of $250,000 or more require approval by two authorized "
              "officers. Wires of $10,000 or more by phone require a callback.", "8. Wire Controls"),
    chunk(11, "## 9. Payment Card Data\n\nCard verification values (CVV/CVC) must never be stored after "
              "authorization, in any system.", "9. Payment Card Data"),
    chunk(6, "## 6. Retention\n\nWire transfer records are retained for 5 years after the transfer date.", "6. Retention"),
    chunk(2, "## 2. Data Classification\n\nRestricted data includes card verification values and account numbers.",
          "2. Data Classification"),
]


def test_cites_the_chunk_the_answer_came_from():
    answer = "CVV/CVC values must never be stored after authorization."
    cited = select_citations(answer, CHUNKS, refused=False)
    assert cited[0].chunk_index == 11
    assert all(c.chunk_index != 10 for c in cited)


def test_numbers_are_strong_evidence():
    answer = "Transfers of $250,000 or more need approval from two authorized officers."
    cited = select_citations(answer, CHUNKS, refused=False)
    assert [c.chunk_index for c in cited] == [10]


def test_refusal_cites_nothing():
    answer = "I don't know. The excerpts do not mention a dress code."
    assert is_refusal(answer)
    assert select_citations(answer, CHUNKS, refused=True) == []


def test_citations_only_point_at_retrieved_chunks():
    answer = "Wire records are kept for 5 years; CVV must never be stored."
    retrieved = {(c.source, c.chunk_index) for c in CHUNKS}
    assert all((c.source, c.chunk_index) in retrieved for c in select_citations(answer, CHUNKS, refused=False))


def test_terms_ignore_placeholders_and_stop_words():
    assert terms("[PERSON_1] asked about the $250,000 threshold") == {"asked", "250000", "threshold"}


def test_is_refusal_negative_cases():
    assert not is_refusal("Wire transfers of $250,000 or more require dual control.")
    assert is_refusal("I don’t know based on these excerpts.")  # curly apostrophe
