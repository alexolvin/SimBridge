"""Phone number contracts — single implementation (Rule 1).

Two deliberately separate contracts:

- normalize_e164 — LENIENT normalization for system-side numbers
  (carrier-reported caller IDs, contact lists, blacklist entries).
  These come from the network in varied formats and must not be lost:
    +79261234555, 89261234555, 79261234555, +7 (926) 123-45-55, ...

- parse_destination — STRICT validation for user-submitted destinations
  (Telegram messages, /v1/sms, /v1/call/outgoing). Only the forms the
  user explicitly allows are accepted (msg #48); everything else —
  letters, spaces, dashes, parens, other lengths, the bare '7' prefix —
  is rejected so the bot answers with an error instead of dialing
  garbage or staying silent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


_E164_RE = re.compile(r"^\+\d{1,15}$")
_DIGITS_ONLY = re.compile(r"\d+")


def normalize_e164(raw: str) -> Optional[str]:
    """Normalize a phone number to E.164 format.

    Returns the normalized string, or None if the input is malformed.

    Rules:
    - Strip all non-digit characters except leading '+'
    - Fewer than 7 digits is never a phone number (shortest real
      international numbers are 7 digits, e.g. Tonga/Samoa +676/+685)
      — None (malformed). 2026-08-22: without this, "989" normalized
      to "+989" (formally valid E.164) and was submitted to the modem
      instead of surfacing "Неправильный номер ...".
    - If the result starts with '8' and is 11 digits (Russia), replace with '+'
    - If the result starts with '7' and is 11 digits (Russia), prefix with '+'
    - If the result starts with '+', validate as E.164
    - Otherwise, None (malformed)

    Known limitation: a TRUNCATED Russian number with an explicit '+'
    (e.g. "+798961271", 10 digits) still passes the E.164 shape check —
    distinguishing it from a genuine 10-digit foreign number would need
    country-code semantics, which this normalizer deliberately does not
    encode. Such a submission fails at the modem with a visible
    "Ошибка отправки ..." — never silent.
    """
    if not raw or not raw.strip():
        return None

    stripped = raw.strip()

    # Preserve leading '+' if present
    has_plus = stripped.startswith("+")
    digits = re.sub(r"[^\d]", "", stripped)

    if not digits:
        return None

    if len(digits) < 7:
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


@dataclass(frozen=True)
class Destination:
    """A validated user-submitted call/SMS destination.

    number: canonical form to dial/send to — E.164 for external
        numbers, raw digits for internal network numbers.
    is_internal: True for 4-digit in-network numbers. These dial a
        PJSIP endpoint of the same name, never the GSM modem.
    """
    number: str
    is_internal: bool


_EXT_PLUS_RE = re.compile(r"^\+\d{11,15}$")  # '+': 11-15 digits (excl. +)
_EXT_RU_RE = re.compile(r"^8\d{10,14}$")     # '8': 11-15 digits total
_EXT_SHORT_RE = re.compile(r"^\d{3}$")       # 3-digit local service number (e.g. 100), dialed as-is
_INTERNAL_RE = re.compile(r"^\d{4}$")        # internal network number


def parse_destination(raw: Optional[str]) -> Optional[Destination]:
    """Strictly validate a user-submitted destination (msg #48 spec).

    Allowed forms — and nothing else:

    - '+' followed by 11-15 digits                  -> external, as-is
    - '8' followed by 10-14 digits (11-15 total)    -> external, "+7"+rest
    - 3 digits (local service number, e.g. 100)     -> external, as-is
    - 4 digits                                      -> internal number

    Everything else is rejected: letters, spaces, dashes, parens, other
    lengths, and the bare '7' prefix ("79261234555" is not allowed).

    Returns a Destination, or None if the input is not an allowed form.
    This is the single validator for user input (Rule 1) — distinct from
    normalize_e164, which stays lenient for system-side numbers.
    """
    s = (raw or "").strip()
    if _EXT_PLUS_RE.match(s):
        return Destination(s, is_internal=False)
    if _EXT_RU_RE.match(s):
        return Destination("+7" + s[1:], is_internal=False)
    if _EXT_SHORT_RE.match(s):
        return Destination(s, is_internal=False)
    if _INTERNAL_RE.match(s):
        return Destination(s, is_internal=True)
    return None
