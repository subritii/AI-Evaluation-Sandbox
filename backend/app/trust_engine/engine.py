"""Trust Engine core: detect PII with Presidio, then mask it with typed placeholders.

    detect(text) -> [Detection]       which spans are PII, and what type
    mask(text, detections) -> text    "Call Ana at 555..." -> "Call [PERSON_1] at [PHONE_NUMBER_1]"

Detection uses Presidio's analyzer. Masking is our own: Presidio's anonymizer
configures operators per entity type, so it can't number placeholders by
order of appearance or give a repeated value the same number. We need both
for scrubbed text to stay readable ("[PERSON_1] ... [PERSON_1] again").
"""

import logging
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import version

import spacy
from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.predefined_recognizers import (
    CreditCardRecognizer,
    IbanRecognizer,
    SpacyRecognizer,
    UsSsnRecognizer,
)

from app.trust_engine.recognizers import (
    AccountNumberRecognizer,
    ContextualPhoneRecognizer,
    OfflineEmailRecognizer,
    UsRoutingNumberRecognizer,
)

# Presidio's debug logs can include matched text. Keep them off (rule: no raw PII in logs).
logging.getLogger("presidio-analyzer").setLevel(logging.WARNING)

SPACY_MODEL = "en_core_web_lg"

# The only entity types we mask. Presidio's NER also emits DATE_TIME, LOCATION,
# NRP, and ORGANIZATION; masking those would wreck policy text ("within 30 days").
PII_ENTITIES = (
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SSN",
    "CREDIT_CARD",
    "IBAN_CODE",
    "US_ROUTING_NUMBER",
    "ACCOUNT_NUMBER",
)

# spaCy (OntoNotes) NER labels other than PERSON.
_UNUSED_SPACY_LABELS = (
    "NORP", "FAC", "ORG", "GPE", "LOC", "PRODUCT", "EVENT", "WORK_OF_ART", "LAW",
    "LANGUAGE", "DATE", "TIME", "PERCENT", "MONEY", "QUANTITY", "ORDINAL", "CARDINAL",
)

# Minimum confidence to mask. Context-only recognizers reach exactly 0.4 with a
# context word nearby (Presidio's floor for context-boosted matches) and stay
# below it without one.
SCORE_THRESHOLD = 0.4

# Bump when recognizers, entities, or the threshold change, so stored rows
# show which rules scrubbed them.
RULES_VERSION = "v1"


@dataclass(frozen=True)
class Detection:
    """One PII span. Holds offsets, not the value, so it's safe to log."""

    entity_type: str
    start: int
    end: int
    score: float


@lru_cache(maxsize=1)
def get_analyzer() -> AnalyzerEngine:
    """Build the analyzer once (loading the spaCy model takes a few seconds).

    Raises instead of letting Presidio download a missing model at runtime.
    """
    if not spacy.util.is_package(SPACY_MODEL):
        raise RuntimeError(
            f"spaCy model {SPACY_MODEL} is not installed. It is installed at image build time "
            "(backend/requirements.txt); the Trust Engine never downloads models at runtime."
        )
    nlp_engine = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": SPACY_MODEL}],
            # We only use spaCy NER for PERSON. Mapping just that label (and
            # ignoring the rest) also silences a warning per unmapped label.
            "ner_model_configuration": {
                "model_to_presidio_entity_mapping": {"PERSON": "PERSON", "PER": "PERSON"},
                "labels_to_ignore": list(_UNUSED_SPACY_LABELS),
            },
        }
    ).create_engine()

    # An explicit registry: only the recognizers listed here run.
    registry = RecognizerRegistry(supported_languages=["en"])
    for recognizer in (
        SpacyRecognizer(supported_entities=["PERSON"]),
        OfflineEmailRecognizer(),
        ContextualPhoneRecognizer(),
        UsSsnRecognizer(),
        CreditCardRecognizer(),  # Luhn checksum
        IbanRecognizer(),  # mod-97 checksum + per-country format
        UsRoutingNumberRecognizer(),
        AccountNumberRecognizer(),
    ):
        registry.add_recognizer(recognizer)
    return AnalyzerEngine(registry=registry, nlp_engine=nlp_engine, supported_languages=["en"])


@lru_cache(maxsize=1)
def scrubber_id() -> str:
    """Identifier stored in document_chunks.scrubbed_by, e.g. 'presidio-2.2.364+en_core_web_lg-3.8.0+rules-v1'."""
    return f"presidio-{version('presidio-analyzer')}+{SPACY_MODEL}-{version(SPACY_MODEL)}+rules-{RULES_VERSION}"


def detect(text: str) -> list[Detection]:
    """Return non-overlapping PII detections in `text`, ordered by position."""
    results = get_analyzer().analyze(
        text=text, language="en", entities=list(PII_ENTITIES), score_threshold=SCORE_THRESHOLD
    )
    candidates = [Detection(r.entity_type, r.start, r.end, r.score) for r in results]
    return _resolve_overlaps(candidates)


def _resolve_overlaps(candidates: list[Detection]) -> list[Detection]:
    """Keep the strongest detection wherever spans overlap.

    Presidio only merges overlaps of the same entity type. Different types can
    still overlap (a number seen as both ACCOUNT_NUMBER and US_ROUTING_NUMBER),
    and replacing both would corrupt the text. Priority: higher score, then
    longer span, then earlier start.
    """
    kept: list[Detection] = []
    for d in sorted(candidates, key=lambda d: (-d.score, -(d.end - d.start), d.start)):
        if all(d.end <= k.start or d.start >= k.end for k in kept):
            kept.append(d)
    return sorted(kept, key=lambda d: d.start)


def mask(text: str, detections: list[Detection]) -> str:
    """Replace each detection with a typed, numbered placeholder like [ACCOUNT_NUMBER_1].

    Numbers follow order of appearance per type, and a repeated value reuses
    its number, so relationships in the text survive masking. The mapping is
    not stored anywhere: masking is one-way.
    """
    placeholders: dict[tuple[str, str], str] = {}
    counters: dict[str, int] = {}
    for d in detections:  # already sorted by start
        key = (d.entity_type, text[d.start : d.end])
        if key not in placeholders:
            counters[d.entity_type] = counters.get(d.entity_type, 0) + 1
            placeholders[key] = f"[{d.entity_type}_{counters[d.entity_type]}]"

    # Replace right to left so earlier offsets stay valid.
    masked = text
    for d in reversed(detections):
        masked = masked[: d.start] + placeholders[(d.entity_type, text[d.start : d.end])] + masked[d.end :]
    return masked
