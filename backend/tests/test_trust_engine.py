"""Trust Engine tests: what must be masked, what must NOT be, and the safety rules.

All values are synthetic. These tests load the spaCy model (a few seconds, once per session).
"""

import logging
import socket
from pathlib import Path

import pytest

from app.trust_engine import scrub_for_storage
from app.trust_engine.engine import Detection, _resolve_overlaps, detect, mask, scrubber_id


def entity_types(text: str) -> list[str]:
    return [d.entity_type for d in detect(text)]


def masked(text: str) -> str:
    return scrub_for_storage(text).text


# --- Should be masked ---------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Customer Maria Lopez opened the account.", "Customer [PERSON_1] opened the account."),
        ("Send details to maria.lopez@example.com today.", "Send details to [EMAIL_ADDRESS_1] today."),
        ("Her phone is (212) 555-0187.", "Her phone is [PHONE_NUMBER_1]."),
        ("International contact: +44 20 7946 0958.", "International contact: [PHONE_NUMBER_1]."),
        ("SSN on file: 536-22-1847.", "SSN on file: [US_SSN_1]."),
        ("Card 4111 1111 1111 1111 was charged.", "Card [CREDIT_CARD_1] was charged."),
        ("Pay to IBAN GB82 WEST 1234 5698 7654 32.", "Pay to IBAN [IBAN_CODE_1]."),
        ("Beneficiary IBAN DE89370400440532013000.", "Beneficiary IBAN [IBAN_CODE_1]."),
        ("Use routing number 021000021.", "Use routing number [US_ROUTING_NUMBER_1]."),
        ("ABA 011000015 for the transfer.", "ABA [US_ROUTING_NUMBER_1] for the transfer."),
        ("Deposit to account 4417123456.", "Deposit to account [ACCOUNT_NUMBER_1]."),
        ("Acct no. 000123456789 is overdrawn.", "Acct no. [ACCOUNT_NUMBER_1] is overdrawn."),
    ],
)
def test_masks_pii(text, expected):
    assert masked(text) == expected


def test_routing_and_account_in_one_sentence_get_their_own_types():
    assert (
        masked("Wire to routing number 021000021, account 4417123456.")
        == "Wire to routing number [US_ROUTING_NUMBER_1], account [ACCOUNT_NUMBER_1]."
    )


def test_repeated_value_reuses_its_placeholder_number():
    text = "Maria Lopez called. Later, John Chen and Maria Lopez met."
    assert masked(text) == "[PERSON_1] called. Later, [PERSON_2] and [PERSON_1] met."


# --- Should NOT be masked -----------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        # Fails the Luhn checksum.
        "Card 4111 1111 1111 1112 was declined.",
        # Fails the IBAN mod-97 checksum.
        "IBAN GB00 WEST 1234 5698 7654 32 is invalid.",
        # Passes the ABA checksum but nothing says it's a routing number.
        "Reference 021000021 for the ticket.",
        # Next to "routing" but fails the ABA checksum.
        "Routing number 021000022 was rejected.",
        # A bare 10-digit ID is not a phone or account number without context.
        "Order 4417123456 shipped on Monday.",
        # Well-known invalid SSN; Presidio rejects it by design.
        "Example SSN format: 123-45-6789.",
        # Policy language: amounts, thresholds, durations, section numbers.
        "Wire transfers of $250,000 or more require dual control.",
        "Report incidents within 36 hours; retain records for 5 years.",
        "Card numbers must be truncated to the first six and last four digits.",
        "See section 8 of policy CHB-POL-017, version 3.2.",
        # Organizations and laws are not people.
        "Cobalt Harbor Bank complies with the Gramm-Leach-Bliley Act.",
    ],
)
def test_does_not_mask_non_pii(text):
    assert entity_types(text) == []
    assert masked(text) == text


def test_mock_policy_document_is_unchanged():
    """The policy contains no PII, so scrubbing must not alter a single character."""
    policy = Path(__file__).resolve().parents[2] / "data" / "policies" / "cobalt_harbor_customer_data_policy.md"
    if not policy.exists():
        policy = Path("/data/policies/cobalt_harbor_customer_data_policy.md")
    text = policy.read_text(encoding="utf-8")
    assert detect(text) == []


@pytest.mark.xfail(strict=True, reason="Known gap: spaCy NER tags a sentence-initial 'Email' as PERSON")
def test_known_gap_capitalized_common_word_tagged_as_person():
    assert masked("Email maria.lopez@example.com for details.") == "Email [EMAIL_ADDRESS_1] for details."


# --- Masking mechanics (no model needed) ---------------------------------------

def test_overlap_resolution_keeps_higher_score():
    candidates = [
        Detection("ACCOUNT_NUMBER", 10, 19, 0.4),
        Detection("US_ROUTING_NUMBER", 10, 19, 0.45),
        Detection("PERSON", 0, 5, 0.85),
    ]
    assert _resolve_overlaps(candidates) == [candidates[2], candidates[1]]


def test_mask_replaces_right_to_left_with_varied_lengths():
    text = "ab 123 cd 4567"
    detections = [Detection("X", 3, 6, 1.0), Detection("Y", 10, 14, 1.0)]
    assert mask(text, detections) == "ab [X_1] cd [Y_1]"


# --- Safety rules ---------------------------------------------------------------

def test_scrubbed_by_names_the_real_scrubber():
    result = scrub_for_storage("nothing sensitive here")
    assert result.scrubbed_by == scrubber_id()
    assert result.scrubbed_by.startswith("presidio-") and "passthrough" not in result.scrubbed_by


def test_scrub_makes_no_network_calls(monkeypatch):
    """Rule 3: detection (including email domain validation) works with the network blocked."""
    detect("warm up")  # model loading happens before the block; it reads local files only

    def blocked(*args, **kwargs):
        raise AssertionError("Trust Engine attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    assert masked("Send it to ana.silva@bank-example.co.uk now.") == "Send it to [EMAIL_ADDRESS_1] now."


def test_no_raw_pii_in_logs(caplog):
    """Rule 1: even at DEBUG level, scrubbing never logs the values it finds."""
    secrets = ["Maria Lopez", "536-22-1847", "4111 1111 1111 1111", "021000021"]
    text = f"{secrets[0]}, SSN {secrets[1]}, card {secrets[2]}, routing {secrets[3]}."
    with caplog.at_level(logging.DEBUG):
        result = scrub_for_storage(text)
    assert result.entity_counts == {"PERSON": 1, "US_SSN": 1, "CREDIT_CARD": 1, "US_ROUTING_NUMBER": 1}
    for secret in secrets:
        assert secret not in caplog.text
        assert secret not in result.text
