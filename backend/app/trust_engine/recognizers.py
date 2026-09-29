"""Custom Presidio recognizers for banking identifiers.

How Presidio scores a pattern match:
  1. The regex match starts at the pattern's base score.
  2. `validate_result()` returning True jumps the score to 1.0; False drops it.
     `invalidate_result()` returning True drops it; otherwise the score stays.
  3. The context enhancer adds +0.35 (minimum 0.4) when one of the
     recognizer's context words appears within 5 words before the match
     (or within the configured suffix window after it).
  4. Matches below the engine's score threshold (0.4 here) are discarded.

So a low base score plus context words means "flag only when the text says
what this number is". That's the right behavior for bare digit strings,
which could be anything.
"""

import re

import tldextract
from presidio_analyzer import Pattern, PatternRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts
from presidio_analyzer.predefined_recognizers import EmailRecognizer, PhoneRecognizer

# Base score for a pattern that should only count when a context word is nearby.
# Below the 0.4 threshold alone; context lifts it to 0.4+.
CONTEXT_ONLY_SCORE = 0.05


class UsRoutingNumberRecognizer(PatternRecognizer):
    """US ABA routing transit number: 9 digits, valid Fed prefix and checksum, plus context.

    Replaces Presidio's AbaRoutingRecognizer, whose passing checksum sets the
    score to 1.0 regardless of context. About 1 in 10 random 9-digit numbers
    pass the ABA checksum, so account numbers, undashed SSNs, and reference
    numbers would be masked as routing numbers. Here the checksum only
    *rejects* (invalidate_result); context words decide whether it's flagged.
    """

    ENTITY = "US_ROUTING_NUMBER"
    # First two digits must be a Federal Reserve routing symbol:
    # 00 (US government), 01-12, 21-32 (thrift), 61-72 (electronic), 80 (traveler's checks).
    PATTERNS = [
        Pattern(
            "ABA routing number",
            r"\b(?:0[0-9]|1[0-2]|2[1-9]|3[0-2]|6[1-9]|7[0-2]|80)\d{7}\b",
            # Slightly above the account recognizer, so a checksum-valid number
            # next to "routing" wins over a generic account match on the same digits.
            CONTEXT_ONLY_SCORE + 0.05,
        )
    ]
    # Presidio matches context against spaCy *lemmas* by substring, and spaCy
    # lemmatizes "routing" to "rout", so "rout" is the entry that catches it.
    # (It also matches "routine"; acceptable, since the checksum must pass too.)
    CONTEXT = ["rout", "aba", "rtn", "transit"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity=self.ENTITY, patterns=self.PATTERNS, context=self.CONTEXT, name="UsRoutingNumberRecognizer"
        )

    def invalidate_result(self, pattern_text: str) -> bool:
        """Reject numbers that fail the ABA checksum (weights 3, 7, 1 repeating)."""
        digits = [int(d) for d in pattern_text]
        weighted = sum(d * w for d, w in zip(digits, [3, 7, 1] * 3))
        return weighted % 10 != 0


class AccountNumberRecognizer(PatternRecognizer):
    """Bank account number: a 6-17 digit string flagged only when context says it's an account.

    Replaces Presidio's UsBankRecognizer (same idea) so the context words are
    ours to tune and the entity name matches our placeholders.
    """

    ENTITY = "ACCOUNT_NUMBER"
    PATTERNS = [Pattern("Account number (context required)", r"\b\d{6,17}\b", CONTEXT_ONLY_SCORE)]
    CONTEXT = ["account", "acct", "checking", "savings", "deposit", "a/c"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity=self.ENTITY, patterns=self.PATTERNS, context=self.CONTEXT, name="AccountNumberRecognizer"
        )


# Uses tldextract's bundled public-suffix snapshot. The default extractor
# downloads the list from publicsuffix.org on first use: a runtime network call.
_OFFLINE_TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


class OfflineEmailRecognizer(EmailRecognizer):
    """Presidio's email recognizer with domain validation that never touches the network."""

    def validate_result(self, pattern_text: str) -> bool:
        return _OFFLINE_TLD_EXTRACT(pattern_text).fqdn != ""


class ContextualPhoneRecognizer(PhoneRecognizer):
    """Presidio's phone recognizer, but bare digit runs need context to count.

    The stock recognizer scores any string python-phonenumbers can parse at
    0.4 (our threshold), so "account 4417123456" and "routing 021000021" were
    masked as phone numbers. Its context words also include "number", which
    *boosted* phone scores for "routing number ...". Formatted numbers
    ("(212) 555-0187", "+44 20 7946 0958") keep the stock score; bare digits
    drop to the context-only score and count only near phone words.
    """

    # Not "call": Presidio builds the context window from non-stop-words, and
    # "call" is a spaCy stop word, so it can never match. Known gap: a bare
    # digit run after "call" alone isn't masked (measured in the detector eval).
    CONTEXT = ["phone", "telephone", "tel", "cell", "mobile", "fax", "contact"]

    def __init__(self) -> None:
        super().__init__(context=self.CONTEXT, name="ContextualPhoneRecognizer")

    def analyze(self, text: str, entities: list[str], nlp_artifacts: NlpArtifacts = None) -> list[RecognizerResult]:
        results = super().analyze(text, entities, nlp_artifacts)
        for result in results:
            # .strip(): python-phonenumbers can include surrounding whitespace in
            # the span (" 9826204505"), which would dodge a bare-digit check.
            if re.fullmatch(r"\d+", text[result.start : result.end].strip()):
                result.score = CONTEXT_ONLY_SCORE
        return results
