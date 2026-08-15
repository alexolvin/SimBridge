"""SMS error vocabulary — localized, user-facing messages.

Distinguishes "we could not submit" from "submitted but not delivered."
Based on the GPT document's error vocabulary (§17), localized to Russian.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class SMSErrorType(Enum):
    """Categorized SMS errors with localized messages."""

    # Submit-time errors
    NUMBER_MISSING = "Номер не указан"
    NUMBER_MALFORMED = "Некорректный формат номера"
    MODEM_UNAVAILABLE = "Модем недоступен"
    NO_GSM_REGISTRATION = "Модем не зарегистрирован в сети"
    SIM_UNAVAILABLE = "SIM-карта недоступна"
    SEND_FAILED = "Ошибка отправки SMS"
    BLACKLISTED = "Номер в черном списке"
    DENIED = "Недостаточно прав"

    # Delivery-time errors
    DELIVERY_FAILED = "SMS не доставлена"
    DELIVERY_EXPIRED = "SMS истекла (не доставлена)"

    # Generic
    UNKNOWN = "Неизвестная ошибка"


    @property
    def is_submit_error(self) -> bool:
        """Whether this error prevents submission (vs delivery failure)."""
        return self in {
            SMSErrorType.NUMBER_MISSING,
            SMSErrorType.NUMBER_MALFORMED,
            SMSErrorType.MODEM_UNAVAILABLE,
            SMSErrorType.NO_GSM_REGISTRATION,
            SMSErrorType.SIM_UNAVAILABLE,
            SMSErrorType.SEND_FAILED,
            SMSErrorType.BLACKLISTED,
            SMSErrorType.DENIED,
        }

    @property
    def message(self) -> str:
        return self.value


def asterisk_sms_error_to_type(error_msg: str) -> SMSErrorType:
    """Map Asterisk/chan_dongle error messages to SMSErrorType.

    Asterisk DongleSendSMS returns various error strings. Map them
    to user-friendly categories.
    """
    err = error_msg.lower() if error_msg else ""

    if "not registered" in err or "no network" in err or "unregistered" in err:
        return SMSErrorType.NO_GSM_REGISTRATION
    if "sim" in err and ("not ready" in err or "missing" in err or "error" in err):
        return SMSErrorType.SIM_UNAVAILABLE
    if "busy" in err or "unavailable" in err or "no modem" in err:
        return SMSErrorType.MODEM_UNAVAILABLE
    if "fail" in err or "error" in err:
        return SMSErrorType.SEND_FAILED

    return SMSErrorType.UNKNOWN
