"""Trust Engine: PII detection and masking.

Everything written to the vector table goes through `scrub_for_storage()`.
Keeping a single chokepoint means callers never touch Presidio directly, and
`scrubbed_by` on every stored row names the exact scrubber that produced it.
"""

from collections import Counter
from dataclasses import dataclass, field

from app.trust_engine.engine import detect, mask, scrubber_id


@dataclass(frozen=True)
class ScrubResult:
    """Masked text, the scrubber that produced it, and how many of each entity type were masked.

    `entity_counts` holds types and counts only, never values, so it's safe to log.
    """

    text: str
    scrubbed_by: str
    entity_counts: dict[str, int] = field(default_factory=dict)


def scrub_for_storage(text: str) -> ScrubResult:
    """Mask PII in `text` before it is embedded or stored."""
    detections = detect(text)
    return ScrubResult(
        text=mask(text, detections),
        scrubbed_by=scrubber_id(),
        entity_counts=dict(Counter(d.entity_type for d in detections)),
    )
