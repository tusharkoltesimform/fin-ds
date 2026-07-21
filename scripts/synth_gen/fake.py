"""Format-valid but 100% fake PII helpers (Phase 0 decision: no real personal
data anywhere, safe to share/demo)."""
import random
import string
import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def fake_pan(rng: random.Random) -> str:
    # AAAPL1234C shape: 5 letters, 4 digits, 1 letter. 4th letter ~ holder type,
    # not modeled here -- format-valid, not semantically accurate.
    letters1 = "".join(rng.choices(string.ascii_uppercase, k=5))
    digits = "".join(rng.choices(string.digits, k=4))
    letter2 = rng.choice(string.ascii_uppercase)
    return f"{letters1}{digits}{letter2}"


def fake_aadhaar_token(rng: random.Random) -> str:
    # Tokenized representation, not a raw 12-digit Aadhaar number.
    return "AADT_" + "".join(rng.choices(string.hexdigits.upper()[:16], k=20))


def fake_card_token(rng: random.Random) -> str:
    return "CTOK_" + "".join(rng.choices(string.hexdigits.upper()[:16], k=24))
