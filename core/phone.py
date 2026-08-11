"""E.164 phone number normalizer — single implementation (Rule 1).

All SMS/voice components use this module to normalize phone numbers.
Handles the formats listed in the GPT document:
  - +79261234555  (already E.164)
  - 89261234555   (Russian 8-prefix)
  - 79261234555   (Russian without +)
  - +7 (926) 123-45-55  (formatted)
"""

from __future__ import annotations

import re
from typing import Optional


_E164_RE = re.compile(r"^\+\d{1,15}$")
_DIGITS_ONLY = re.compile(r"\d+")


def normalize_e164(raw: str) -> Optional[str]:
    """Normalize a phone number to E.164 format.

    Returns the normalized string, or None if the input is malformed.

    Rules:
    - Strip all non-digit characters except leading '+'
    - If the result starts with '8' and is 11 digits (Russia), replace with '+'
    - If the result starts with '7' and is 11 digits (Russia), prefix with '+'
    - If the result starts with '+', validate as E.164
    - Otherwise, None (malformed)
    """
    if not raw or not raw.strip():
        return None

    stripped = raw.strip()

    # Preserve leading '+' if present
    has_plus = stripped.startswith("+")
    digits = re.sub(r"[^\d]", "", stripped)

    if not digits:
        return None

    if has_plus:
        # Already has '+', just validate
        result = f"+{digits}"
        if _E164_RE.match(result):
            return result
        return None

    # No '+' prefix — handle Russian national format
    if digits.startswith("8") and len(digits) == 11:
        # 89261234555 -> +79261234555
        return f"+7{digits[1:]}"

    if digits.startswith("7") and len(digits) == 11:
        # 79261234555 -> +79261234555
        return f"+{digits}"

    # Already looks like E.164 without '+' — if it's valid with '+'
    result = f"+{digits}"
    if _E164_RE.match(result):
        return result

    return None


def is_service_number(number: str) -> bool:
    """Check if a number is a short service number (e.g., 900, *100#).

    Service numbers are typically 2-6 digits and not E.164.
    """
    digits = re.sub(r"[^\d]", "", number)
    return 2 <= len(digits) <= 6 and not number.startswith(("+", "7", "8"))
