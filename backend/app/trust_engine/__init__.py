"""Trust Engine: PII detection and masking (implemented in Task 2).

Everything written to the vector table goes through `scrub_for_storage()`.
Keeping a single chokepoint means Task 2 only has to replace this function's
body; ingestion code doesn't change.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Recorded in document_chunks.scrubbed_by so un-scrubbed rows are easy to find:
#   SELECT count(*) FROM document_chunks WHERE scrubbed_by = 'passthrough-stub';
PASSTHROUGH_STUB = "passthrough-stub"

_warned = False


@dataclass(frozen=True)
class ScrubResult:
    """Text after masking, plus the name of the scrubber that produced it."""

    text: str
    scrubbed_by: str


def scrub_for_storage(text: str) -> ScrubResult:
    """Mask PII in `text` before it is embedded or stored.

    TEMPORARY (Task 1): this is a labeled pass-through. It returns the text
    unchanged and tags it `passthrough-stub`. It is safe only because the
    Task 1 mock policy contains no PII. Task 2 replaces it with Presidio.
    """
    global _warned
    if not _warned:
        # Logs a fixed message only, never the text itself.
        logger.warning(
            "Trust Engine not implemented yet (Task 2): text is stored UNMASKED "
            "and tagged scrubbed_by='%s'. Ingest only PII-free documents.",
            PASSTHROUGH_STUB,
        )
        _warned = True
    return ScrubResult(text=text, scrubbed_by=PASSTHROUGH_STUB)
