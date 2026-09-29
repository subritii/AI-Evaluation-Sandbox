"""Generate a labeled synthetic PII dataset for the detector evaluation.

Every value is fake (Faker + random digits). Records are built from
bank-style templates; each PII value's character span is recorded as the
text is assembled, so labels are exact by construction.

The mix is deliberate:
  - "standard" templates: clear context words, common formats
  - "hard" templates: abbreviations, international names, bare digits,
    identifiers with no context words (expected misses, kept on purpose)
  - "negative" templates: no PII, but lots of numbers that look like it
    (Luhn-invalid cards, bad IBAN checksums, order/ticket IDs, policy amounts)

Usage:
    .venv/bin/python scripts/generate_dataset.py              # 1000 records, seed 42
    .venv/bin/python scripts/generate_dataset.py -n 2000 --seed 7

Writes data/generated/pii_dataset.jsonl (gitignored; regenerate from the seed).
"""

import argparse
import json
import random
from collections import Counter
from pathlib import Path

from faker import Faker

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "data" / "generated" / "pii_dataset.jsonl"

# Share of records drawn from the negative (no-PII) templates.
NEGATIVE_SHARE = 0.15

# Real US area codes, so python-phonenumbers treats the numbers as plausible.
AREA_CODES = [201, 212, 213, 305, 312, 404, 415, 469, 512, 617, 646, 702, 713, 718, 818, 917]


class Values:
    """Fake value generators. One seeded instance per dataset for reproducibility."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        Faker.seed(seed)
        self.us = Faker("en_US")
        # Mostly US names, plus other locales a US bank's customers commonly have.
        self.names = Faker(["en_US", "en_US", "en_GB", "es_MX", "de_DE", "fr_FR", "en_IN"])
        self.iban_fakers = [Faker("en_GB"), Faker("de_DE"), Faker("fr_FR"), Faker("es_ES"), Faker("nl_NL")]

    def digits(self, n: int) -> str:
        return "".join(str(self.rng.randint(0, 9)) for _ in range(n))

    def person(self) -> str:
        return self.names.first_name() + " " + self.names.last_name()

    def email(self) -> str:
        return self.us.email()

    def phone(self, style: str | None = None) -> str:
        area, exchange, line = self.rng.choice(AREA_CODES), self.rng.randint(200, 999), self.digits(4)
        style = style or self.rng.choice(["paren", "dash", "dot", "intl"])
        return {
            "paren": f"({area}) {exchange}-{line}",
            "dash": f"{area}-{exchange}-{line}",
            "dot": f"{area}.{exchange}.{line}",
            "intl": f"+1 {area} {exchange} {line}",
            "bare": f"{area}{exchange}{line}",
        }[style]

    def ssn(self, dashed: bool = True) -> str:
        value = self.us.ssn()
        return value if dashed else value.replace("-", "")

    def card(self) -> str:
        number = self.us.credit_card_number()
        if len(number) == 16 and self.rng.random() < 0.5:
            sep = self.rng.choice([" ", "-"])
            return sep.join(number[i : i + 4] for i in range(0, 16, 4))
        return number

    def iban(self) -> str:
        value = self.rng.choice(self.iban_fakers).iban()
        if self.rng.random() < 0.5:
            return " ".join(value[i : i + 4] for i in range(0, len(value), 4))
        return value

    def routing(self) -> str:
        return self.us.aba()

    def account(self) -> str:
        return self.digits(self.rng.randint(8, 12))

    # --- look-alikes for negatives ---
    def invalid_card(self) -> str:
        number = self.us.credit_card_number(card_type="visa16")
        return number[:-1] + str((int(number[-1]) + 1) % 10)  # breaks Luhn

    def invalid_iban(self) -> str:
        value = self.rng.choice(self.iban_fakers).iban()
        return value[:2] + "00" + value[4:]  # "00" is never a valid check digit pair

    def amount(self) -> str:
        return f"${self.rng.choice([1, 5, 10, 25, 50, 250])},{self.rng.choice(['000', '500'])}"

    def date(self) -> str:
        return self.us.date_between("-2y", "today").strftime(self.rng.choice(["%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d"]))


def P(entity_type: str, value: str) -> tuple[str, str]:
    """A labeled PII slot inside a template."""
    return (entity_type, value)


# Each template returns a list of plain strings and labeled P(...) slots.
STANDARD = {
    "service_note": lambda v: ["Customer ", P("PERSON", v.person()), " called about account ",
                               P("ACCOUNT_NUMBER", v.account()), "; callback number ", P("PHONE_NUMBER", v.phone()), "."],
    "email_update": lambda v: [P("PERSON", v.person()), " asked us to update the email on file to ",
                               P("EMAIL_ADDRESS", v.email()), "."],
    "wire_request": lambda v: ["Wire request: routing number ", P("US_ROUTING_NUMBER", v.routing()), ", account number ",
                               P("ACCOUNT_NUMBER", v.account()), ", beneficiary ", P("PERSON", v.person()), "."],
    "intl_wire": lambda v: ["Send ", v.amount(), " to IBAN ", P("IBAN_CODE", v.iban()), " for ", P("PERSON", v.person()), "."],
    "kyc_note": lambda v: ["KYC verified for ", P("PERSON", v.person()), ", SSN ", P("US_SSN", v.ssn()), ", phone ",
                           P("PHONE_NUMBER", v.phone()), "."],
    "card_dispute": lambda v: [P("PERSON", v.person()), " disputes a charge on card number ", P("CREDIT_CARD", v.card()),
                               " from ", v.date(), "."],
    "aba_short": lambda v: ["ABA ", P("US_ROUTING_NUMBER", v.routing()), " / acct ", P("ACCOUNT_NUMBER", v.account()), "."],
    "contact_card": lambda v: ["Contact: ", P("PERSON", v.person()), ", ", P("EMAIL_ADDRESS", v.email()), ", mobile ",
                               P("PHONE_NUMBER", v.phone()), "."],
    "direct_deposit": lambda v: ["Set up direct deposit to checking account ", P("ACCOUNT_NUMBER", v.account()),
                                 " with routing number ", P("US_ROUTING_NUMBER", v.routing()), "."],
    "ssn_form": lambda v: ["Social security number: ", P("US_SSN", v.ssn()), ". Applicant: ", P("PERSON", v.person()), "."],
    "card_on_file": lambda v: ["Card on file ", P("CREDIT_CARD", v.card()), " expires next month; notify ",
                               P("EMAIL_ADDRESS", v.email()), "."],
    "mixed_with_ids": lambda v: [P("PERSON", v.person()), " reported order ", v.digits(10), " missing; reach them at ",
                                 P("PHONE_NUMBER", v.phone()), "."],
}

HARD = {
    # No context words at all: the recognizers are expected to miss these.
    "no_context_numbers": lambda v: ["Please send the funds to ", P("US_ROUTING_NUMBER", v.routing()), " / ",
                                     P("ACCOUNT_NUMBER", v.account()), " today."],
    # "call" is a spaCy stop word, so it can't act as phone context.
    "bare_phone_call": lambda v: ["Please call ", P("PHONE_NUMBER", v.phone("bare")), " after 5pm."],
    "bare_phone_ctx": lambda v: ["Customer phone ", P("PHONE_NUMBER", v.phone("bare")), " is unverified."],
    "ssn_undashed": lambda v: ["SSN ", P("US_SSN", v.ssn(dashed=False)), " provided at account opening by ",
                               P("PERSON", v.person()), "."],
    "lowercase_name": lambda v: ["spoke with ", P("PERSON", v.person().lower()), " re: statement copy"],
    "rtn_abbrev": lambda v: ["RTN: ", P("US_ROUTING_NUMBER", v.routing()), ". Beneficiary A/C: ",
                             P("ACCOUNT_NUMBER", v.account()), "."],
    "bank_code": lambda v: ["Bank code ", P("US_ROUTING_NUMBER", v.routing()), " was provided by ", P("PERSON", v.person()), "."],
    "name_first": lambda v: [P("PERSON", v.person()), " can be reached at ", P("PHONE_NUMBER", v.phone()), "."],
    "iban_inline": lambda v: ["Beneficiary ", P("IBAN_CODE", v.iban()), " (", P("PERSON", v.person()), ")."],
}

NEGATIVE = {
    "ticket": lambda v: ["Ticket ", v.digits(9), " was escalated to tier 2 on ", v.date(), "."],
    "order": lambda v: ["Order ", v.digits(10), " shipped via ground; tracking updates daily."],
    "invalid_card": lambda v: ["Test card ", v.invalid_card(), " was declined by the processor."],
    "invalid_iban": lambda v: ["The value ", v.invalid_iban(), " failed IBAN validation."],
    "aba_lookalike": lambda v: ["Reference ", v.routing(), " is attached to the dispute file."],
    "policy_amount": lambda v: ["Wire transfers of ", v.amount(), " or more require dual control."],
    "policy_time": lambda v: [f"Report incidents within {v.rng.choice([1, 4, 24, 36])} hours and retain records for "
                              f"{v.rng.choice([2, 3, 5, 7])} years."],
    "branch": lambda v: ["Branch ", v.digits(4), " at ZIP ", v.digits(5), " opens at 9:00 AM."],
    "invoice": lambda v: ["Invoice INV-", v.digits(6), " totals ", v.amount(), " and is due ", v.date(), "."],
    "policy_ref": lambda v: ["Policy CHB-POL-", v.digits(3), " version 3.", v.digits(1), " was approved by the Board Risk Committee."],
    "masked_card": lambda v: ["Card ending in ", v.digits(4), " was reissued."],
}


def build_record(record_id: int, template_name: str, parts: list, difficulty: str) -> dict:
    """Concatenate parts into text, recording the span of every labeled slot."""
    text, spans = "", []
    for part in parts:
        if isinstance(part, tuple):
            entity_type, value = part
            spans.append({"start": len(text), "end": len(text) + len(value), "entity_type": entity_type})
            text += value
        else:
            text += part
    return {"id": record_id, "template": template_name, "difficulty": difficulty, "text": text, "spans": spans}


def generate(n: int, seed: int) -> list[dict]:
    """Generate `n` records: NEGATIVE_SHARE negatives, the rest split ~2:1 standard:hard."""
    values = Values(seed)
    rng = random.Random(seed)
    records = []
    for i in range(n):
        roll = rng.random()
        if roll < NEGATIVE_SHARE:
            pool, difficulty = NEGATIVE, "negative"
        elif roll < NEGATIVE_SHARE + (1 - NEGATIVE_SHARE) * 2 / 3:
            pool, difficulty = STANDARD, "standard"
        else:
            pool, difficulty = HARD, "hard"
        name = rng.choice(sorted(pool))
        records.append(build_record(i, name, pool[name](values), difficulty))
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a labeled synthetic PII dataset.")
    parser.add_argument("-n", type=int, default=1000, help="Number of records")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    records = generate(args.n, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")

    by_difficulty = Counter(r["difficulty"] for r in records)
    by_entity = Counter(s["entity_type"] for r in records for s in r["spans"])
    print(f"Wrote {len(records)} records to {args.out.relative_to(REPO_ROOT)} (seed {args.seed})")
    print("Records by difficulty:", dict(sorted(by_difficulty.items())))
    print("Labeled spans by entity:", dict(sorted(by_entity.items())))


if __name__ == "__main__":
    main()
