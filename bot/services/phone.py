"""E.164 phone-number validation and normalisation (PRD §FR-8).

The assignee onboarding flow must persist a *validated* E.164 number
before it will send the meeting-invite template. WhatsApp itself wants
the number with country code and no leading "+", but our canonical
stored form keeps the "+" (E.164). Two helpers cover both needs:

    normalize_e164(raw, default_region) -> "+9198XXXXXXXX" | None
    to_wa_id(e164)                       -> "9198XXXXXXXX"  (no +)

If the optional `phonenumbers` library is installed we use it (correct
length/region rules per country). Otherwise we fall back to a permissive
regex that at least enforces the E.164 shape (8–15 digits).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import phonenumbers  # type: ignore
    _HAVE_PHONENUMBERS = True
except ImportError:  # pragma: no cover - depends on install
    _HAVE_PHONENUMBERS = False

# E.164: leading +, country code starting 1-9, total 8–15 digits.
_E164_RX = re.compile(r"^\+[1-9]\d{7,14}$")

# Default region for bare local numbers ("9876543210" → +91...).
DEFAULT_REGION = "IN"


def normalize_e164(raw: str, default_region: str = DEFAULT_REGION) -> Optional[str]:
    """Return a canonical ``+<countrycode><number>`` string, or None.

    Accepts user input in many shapes ("+91 98xxx", "098xxx", "98xxx",
    "919xxxxx"). Returns None when the number can't be made valid.
    """
    if not raw:
        return None
    raw = raw.strip()

    if _HAVE_PHONENUMBERS:
        try:
            # If the user typed a leading +, region is ignored by the lib.
            region = None if raw.startswith("+") else default_region
            parsed = phonenumbers.parse(raw, region)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.E164
                )
            logger.info(f"Rejected invalid phone {raw!r} (region={default_region})")
            return None
        except Exception as e:  # NumberParseException etc.
            logger.info(f"phonenumbers could not parse {raw!r}: {e}")
            return None

    # ── Fallback: regex-only normalisation ──────────────────────
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("+"):
        candidate = digits
    elif digits.startswith("00"):
        candidate = "+" + digits[2:]
    elif digits.startswith("0"):
        # Local number with trunk prefix — assume default region (IN=+91)
        cc = {"IN": "91", "US": "1", "GB": "44"}.get(default_region, "91")
        candidate = "+" + cc + digits[1:]
    elif len(digits) == 10 and default_region == "IN":
        candidate = "+91" + digits
    else:
        candidate = "+" + digits
    return candidate if _E164_RX.match(candidate) else None


def is_valid_e164(raw: str, default_region: str = DEFAULT_REGION) -> bool:
    """True if ``raw`` can be normalised to a valid E.164 number."""
    return normalize_e164(raw, default_region) is not None


def to_wa_id(e164_or_raw: str, default_region: str = DEFAULT_REGION) -> Optional[str]:
    """Return the WhatsApp wa_id form (digits only, no '+')."""
    norm = normalize_e164(e164_or_raw, default_region)
    return norm.lstrip("+") if norm else None
