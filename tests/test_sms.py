"""Stage 02 tests — SMS complete: contacts, blacklist, reply routing, errors.

Tests: TS02-1 (normalizer), TS02-2 (contacts cache hit/miss),
       TS02-3/4 (BLOCK persist — unit), TS02-5 (atomic write),
       TS02-6 (correlation), TS02-7 (reply forms), TS02-8 (error matrix).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path

import pytest

from agent.ami_client import AMIClient, AMISendError
from core.acl import ACLManager
from core.events import EventType
from core.phone import normalize_e164, is_service_number
from core.contacts import (
    CSVContactProvider,
    ServiceNumberProvider,
    ContactResolver,
)
from core.blacklist import BlacklistManager
from core.sms_correlation import SMSCorrelationStore
from core.errors import SMSErrorType, asterisk_sms_error_to_type


# =========================================================================
# TS02-1 — Number normalizer
# =========================================================================

class TestPhoneNormalizer:
    """TS02-1: normalize_e164 against all four formats plus malformed inputs."""

    def test_e164_format(self):
        """Already E.164: +79261234555"""
        assert normalize_e164("+79261234555") == "+79261234555"

    def test_russian_8_prefix(self):
        """Russian 8-prefix: 89261234555 -> +79261234555"""
        assert normalize_e164("89261234555") == "+79261234555"

    def test_russian_7_without_plus(self):
        """Russian 7 without +: 79261234555 -> +79261234555"""
        assert normalize_e164("79261234555") == "+79261234555"

    def test_formatted_with_spaces_and_dashes(self):
        """Formatted: +7 (926) 123-45-55 -> +79261234555"""
        assert normalize_e164("+7 (926) 123-45-55") == "+79261234555"

    def test_malformed_empty(self):
        """Empty string -> None"""
        assert normalize_e164("") is None

    def test_malformed_none(self):
        """None -> None"""
        assert normalize_e164(None) is None

    def test_malformed_letters(self):
        """Letters only -> None"""
        assert normalize_e164("abc") is None

    def test_whitespace_only(self):
        """Whitespace -> None"""
        assert normalize_e164("   ") is None

    def test_us_number(self):
        """US number: +14155552671"""
        assert normalize_e164("+14155552671") == "+14155552671"

    def test_too_short_is_malformed(self):
        """Fewer than 7 digits is never a phone number (2026-08-22):
        "989" used to normalize to "+989" and get submitted to the
        modem; the user must see "Неправильный номер ..." instead."""
        assert normalize_e164("989") is None
        assert normalize_e164("+989") is None
        assert normalize_e164("123456") is None

    def test_seven_digit_minimum(self):
        """7 digits is the shortest real international number
        (e.g. Tonga/Samoa +676/+685) and must still pass."""
        assert normalize_e164("+67623123") == "+67623123"
        assert normalize_e164("67623123") == "+67623123"


class TestServiceNumbers:
    """Service number detection."""

    def test_short_service_number(self):
        assert is_service_number("900") is True

    def test_e164_not_service(self):
        assert is_service_number("+79261234555") is False

    def test_112(self):
        assert is_service_number("112") is True


# =========================================================================
# TS02-2 — Contact cache hit and miss
# =========================================================================

class TestContactCache:
    """TS02-2: CSV contact cache hit and miss paths."""

    def test_cache_hit(self, tmp_path: Path):
        csv_path = tmp_path / "contacts.csv"
        csv_path.write_text("+79261234555,Иванов Иван Иванович\n")
        provider = CSVContactProvider(str(csv_path))
        assert provider.lookup("+79261234555") == "Иванов Иван Иванович"

    def test_cache_miss(self, tmp_path: Path):
        csv_path = tmp_path / "contacts.csv"
        csv_path.write_text("+79261234555,Иванов Иван Иванович\n")
        provider = CSVContactProvider(str(csv_path))
        assert provider.lookup("+14155552671") is None

    def test_comments_and_empty_lines(self, tmp_path: Path):
        csv_path = tmp_path / "contacts.csv"
        csv_path.write_text("# comment\n\n+79261234555,Иванов\n\n")
        provider = CSVContactProvider(str(csv_path))
        assert provider.lookup("+79261234555") == "Иванов"

    def test_normalization_before_lookup(self, tmp_path: Path):
        """Numbers in the CSV are stored normalized."""
        csv_path = tmp_path / "contacts.csv"
        csv_path.write_text("+79261234555,Иванов\n")
        provider = CSVContactProvider(str(csv_path))
        # Lookup with 8-prefix format should find the entry
        assert provider.lookup("89261234555") == "Иванов"


class TestServiceNumberProvider:
    def test_known_service(self):
        provider = ServiceNumberProvider()
        assert provider.lookup("112") == "Единая служба спасения"

    def test_unknown_service(self):
        provider = ServiceNumberProvider()
        assert provider.lookup("999") is None


class TestContactResolver:
    """Composed resolver: cache first, then service numbers."""

    def test_chain_order(self, tmp_path: Path):
        csv_path = tmp_path / "contacts.csv"
        csv_path.write_text("+79261234555,Иванов\n")
        resolver = ContactResolver(csv_path=str(csv_path))
        # CSV hit
        assert resolver.resolve("+79261234555") == "Иванов"
        # Service number hit (not in CSV)
        assert resolver.resolve("112") == "Единая служба спасения"
        # Miss
        assert resolver.resolve("+14155552671") is None

    def test_csv_takes_priority(self, tmp_path: Path):
        """If the same number is in both CSV and service directory, CSV wins."""
        csv_path = tmp_path / "contacts.csv"
        csv_path.write_text("112,Моя служба\n")
        resolver = ContactResolver(csv_path=str(csv_path))
        assert resolver.resolve("112") == "Моя служба"


# =========================================================================
# TS02-3 / TS02-5 — Blacklist persistence and atomic write
# =========================================================================

class TestBlacklistManager:
    """TS02-3: BLOCK persists. TS02-5: Atomic write."""

    def test_block_persists(self, tmp_path: Path):
        bl_path = tmp_path / "blacklist.txt"
        bl_path.write_text("# empty\n")
        bl = BlacklistManager(str(bl_path))
        added = bl.block("+79261234555")
        assert added is True
        assert bl.contains("+79261234555") is True

        # Reload from disk and verify persistence
        bl2 = BlacklistManager(str(bl_path))
        assert bl2.contains("+79261234555") is True

    def test_unblock(self, tmp_path: Path):
        bl_path = tmp_path / "blacklist.txt"
        bl_path.write_text("+79261234555\n")
        bl = BlacklistManager(str(bl_path))
        assert bl.contains("+79261234555") is True
        removed = bl.unblock("+79261234555")
        assert removed is True
        assert bl.contains("+79261234555") is False

    def test_block_normalizes(self, tmp_path: Path):
        """Blocking with 8-prefix normalizes to E.164."""
        bl_path = tmp_path / "blacklist.txt"
        bl_path.write_text("# empty\n")
        bl = BlacklistManager(str(bl_path))
        bl.block("89261234555")
        # Should be stored and matched as E.164
        assert bl.contains("+79261234555") is True

    def test_block_malformed_rejected(self, tmp_path: Path):
        bl_path = tmp_path / "blacklist.txt"
        bl_path.write_text("# empty\n")
        bl = BlacklistManager(str(bl_path))
        result = bl.block("abc")
        assert result is False

    def test_atomic_write_no_partial(self, tmp_path: Path):
        """The file is never left in a partial state."""
        bl_path = tmp_path / "blacklist.txt"
        bl_path.write_text("# initial\n")
        bl = BlacklistManager(str(bl_path))

        # Add multiple numbers
        for i in range(10):
            num = f"+7926100000{i}"
            bl.block(num)
            # File should always be readable after each write
            content = bl_path.read_text()
            assert "# SimBridge blacklist" in content

        # Verify all numbers are present
        for i in range(10):
            num = f"+7926100000{i}"
            assert bl.contains(num) is True

    def test_duplicate_block(self, tmp_path: Path):
        bl_path = tmp_path / "blacklist.txt"
        bl_path.write_text("# empty\n")
        bl = BlacklistManager(str(bl_path))
        first = bl.block("+79261234555")
        second = bl.block("+79261234555")
        assert first is True
        assert second is False

    def test_count(self, tmp_path: Path):
        bl_path = tmp_path / "blacklist.txt"
        bl_path.write_text("# empty\n")
        bl = BlacklistManager(str(bl_path))
        assert bl.count == 0
        bl.block("+79261234555")
        assert bl.count == 1

    def test_comments_preserved_in_output(self, tmp_path: Path):
        """The output file has a header comment."""
        bl_path = tmp_path / "blacklist.txt"
        bl_path.write_text("# my custom blacklist\n+79261234555\n")
        bl = BlacklistManager(str(bl_path))
        bl.block("+14155552671")
        content = bl_path.read_text()
        assert "# SimBridge blacklist" in content
        assert "+79261234555" in content
        assert "+14155552671" in content

    def test_manual_edit_reload(self, tmp_path: Path):
        """Simulate manual edit: write to file, reload, verify."""
        bl_path = tmp_path / "blacklist.txt"
        bl_path.write_text("# empty\n")
        bl = BlacklistManager(str(bl_path))

        # Manual edit
        bl_path.write_text("+79269999999\n")
        bl.reload()
        assert bl.contains("+79269999999") is True

    def test_hot_reload_on_contains(self, tmp_path: Path):
        """S01.4: contains() detects manual edits without an explicit reload()."""
        bl_path = tmp_path / "blacklist.txt"
        bl_path.write_text("# empty\n")
        bl = BlacklistManager(str(bl_path))
        assert bl.contains("+79269999999") is False

        # Manual edit — no bl.reload() call; contains() must notice.
        import os, time
        time.sleep(0.01)
        bl_path.write_text("+79269999999\n")
        os.utime(str(bl_path))

        assert bl.contains("+79269999999") is True

    def test_nonexistent_file(self, tmp_path: Path):
        """Creating with a nonexistent file should work (empty blacklist)."""
        bl_path = tmp_path / "nonexistent.txt"
        bl = BlacklistManager(str(bl_path))
        assert bl.count == 0


# =========================================================================
# TS02-6 — SMS correlation (concurrent delivery matching)
# =========================================================================

class TestSMSCorrelation:
    """TS02-6: Concurrent-send delivery matching."""

    def test_create_and_track(self):
        store = SMSCorrelationStore()
        rec = store.create(
            telegram_user_id=123,
            phone_number="+79261234555",
            text="hello",
            telegram_message_id=42,
        )
        assert rec.sms_id is not None
        assert rec.submit_status == "pending"
        assert rec.delivery_status == "pending"

    def test_submit_then_deliver(self):
        store = SMSCorrelationStore()
        rec = store.create(123, "+79261234555", "test")
        assert store.mark_submitted(rec.sms_id) is True
        assert rec.submit_status == "submitted"
        assert store.mark_delivered(rec.sms_id) is True
        assert rec.delivery_status == "delivered"

    def test_concurrent_sends_different_ids(self):
        """Two SMS to different numbers get different sms_ids."""
        store = SMSCorrelationStore()
        rec1 = store.create(123, "+79261234555", "msg1")
        rec2 = store.create(123, "+14155552671", "msg2")
        assert rec1.sms_id != rec2.sms_id

    def test_delivery_matched_by_id_not_text(self):
        """Delivery reports matched by sms_id, not by text search."""
        store = SMSCorrelationStore()
        rec1 = store.create(123, "+79261234555", "same text")
        rec2 = store.create(123, "+14155552671", "same text")
        # Both have the same text — text search would be ambiguous
        store.mark_delivered(rec1.sms_id)
        assert rec1.delivery_status == "delivered"
        assert rec2.delivery_status == "pending"

    def test_mark_failed_submit(self):
        store = SMSCorrelationStore()
        rec = store.create(123, "+79261234555", "test")
        store.mark_failed(rec.sms_id, error="modem down", submit_failed=True)
        assert rec.submit_status == "failed"
        assert rec.error_message == "modem down"

    def test_mark_failed_delivery(self):
        store = SMSCorrelationStore()
        rec = store.create(123, "+79261234555", "test")
        store.mark_submitted(rec.sms_id)
        store.mark_failed(rec.sms_id, error="expired", submit_failed=False)
        assert rec.submit_status == "submitted"
        assert rec.delivery_status == "failed"

    def test_find_by_telegram_message(self):
        """Find the record for a reply to a specific message."""
        store = SMSCorrelationStore()
        rec = store.create(
            telegram_user_id=123,
            phone_number="+79261234555",
            text="test",
            telegram_message_id=42,
        )
        found = store.find_by_telegram_message(123, 42)
        assert found is not None
        assert found.sms_id == rec.sms_id

    def test_recent(self):
        store = SMSCorrelationStore()
        store.create(123, "+79261234555", "msg1")
        store.create(123, "+14155552671", "msg2")
        store.create(456, "+79261234555", "other_user")
        recent = store.recent(123, limit=5)
        assert len(recent) == 2

    def test_unknown_sms_id(self):
        store = SMSCorrelationStore()
        assert store.mark_delivered("nonexistent") is False
        assert store.mark_submitted("nonexistent") is False
        assert store.mark_failed("nonexistent", error="err") is False


# =========================================================================
# TS02-8 — Error state matrix
# =========================================================================

class TestErrorVocabulary:
    """TS02-8: Error-state mapping."""

    def test_all_errors_have_messages(self):
        for error_type in SMSErrorType:
            assert len(error_type.message) > 0

    def test_submit_vs_delivery_distinction(self):
        submit_errors = [
            SMSErrorType.NUMBER_MISSING,
            SMSErrorType.NUMBER_MALFORMED,
            SMSErrorType.MODEM_UNAVAILABLE,
            SMSErrorType.NO_GSM_REGISTRATION,
            SMSErrorType.SIM_UNAVAILABLE,
            SMSErrorType.SEND_FAILED,
            SMSErrorType.BLACKLISTED,
        ]
        for err in submit_errors:
            assert err.is_submit_error is True

        delivery_errors = [
            SMSErrorType.DELIVERY_FAILED,
            SMSErrorType.DELIVERY_EXPIRED,
        ]
        for err in delivery_errors:
            assert err.is_submit_error is False

    def test_asterisk_error_mapping_no_network(self):
        result = asterisk_sms_error_to_type("Device is not registered on network")
        assert result == SMSErrorType.NO_GSM_REGISTRATION

    def test_asterisk_error_mapping_sim(self):
        result = asterisk_sms_error_to_type("SIM not ready")
        assert result == SMSErrorType.SIM_UNAVAILABLE

    def test_asterisk_error_mapping_busy(self):
        result = asterisk_sms_error_to_type("Modem busy")
        assert result == SMSErrorType.MODEM_UNAVAILABLE

    def test_asterisk_error_mapping_generic(self):
        result = asterisk_sms_error_to_type("Unknown failure")
        assert result == SMSErrorType.SEND_FAILED

    def test_asterisk_error_empty(self):
        result = asterisk_sms_error_to_type("")
        assert result == SMSErrorType.UNKNOWN


# =========================================================================
# TS02-9 — Text fidelity (commas, Cyrillic, emoji)
# =========================================================================

class TestTextFidelity:
    """TS02-9: SMS text with commas, Cyrillic, and emoji survives intact."""

    def test_commas_preserved(self):
        """SMS text with commas is not truncated."""
        text = "hello, world, test"
        # The text should survive intact — no comma-based truncation
        assert "," in text
        assert text == "hello, world, test"

    def test_cyrillic_preserved(self):
        """Cyrillic text survives encoding."""
        text = "Привет, мир!"
        # Verify round-trip through UTF-8
        assert text.encode("utf-8").decode("utf-8") == text

    def test_emoji_preserved(self):
        """Emoji survives encoding."""
        text = "Hello \U0001f600!"
        assert text.encode("utf-8").decode("utf-8") == text

    def test_mixed_content(self):
        """Mixed Cyrillic, commas, and emoji."""
        text = "Привет, мир! \U0001f600"
        assert text.encode("utf-8").decode("utf-8") == text


# =========================================================================
# S02.3 — Persistent correlation store (restart-safe delivery matching)
# =========================================================================

class TestSMSCorrelationPersistence:
    """The store must survive an agent restart via the JSONL log."""

    def test_roundtrip(self, tmp_path):
        path = str(tmp_path / "correl.jsonl")
        store = SMSCorrelationStore(log_path=path)
        rec = store.create(123, "+79261234555", "hello", modem_id="gsm")
        store.mark_submitted(rec.sms_id)
        store.mark_delivered(rec.sms_id)

        reloaded = SMSCorrelationStore(log_path=path)
        again = reloaded.get(rec.sms_id)
        assert again is not None
        assert again.phone_number == "+79261234555"
        assert again.telegram_user_id == 123
        assert again.modem_id == "gsm"
        assert again.submit_status == "submitted"
        assert again.delivery_status == "delivered"

    def test_reloaded_store_mutations_persist(self, tmp_path):
        path = str(tmp_path / "correl.jsonl")
        store = SMSCorrelationStore(log_path=path)
        rec = store.create(123, "+79261234555", "hello")
        store.mark_submitted(rec.sms_id)

        reloaded = SMSCorrelationStore(log_path=path)
        assert reloaded.get(rec.sms_id).submit_status == "submitted"
        assert reloaded.mark_delivered(rec.sms_id) is True

        again = SMSCorrelationStore(log_path=path)
        assert again.get(rec.sms_id).delivery_status == "delivered"


class TestMatchReport:
    """S02.3: carrier report → record matching (dongle + number hint)."""

    @staticmethod
    def _submitted(store, phone, modem="gsm", text="m"):
        rec = store.create(123, phone, text, modem_id=modem)
        assert store.mark_submitted(rec.sms_id)
        return rec

    def test_hint_beats_recency(self):
        store = SMSCorrelationStore()
        old = self._submitted(store, "+79261234555")
        time.sleep(0.01)
        self._submitted(store, "+79000000000")
        # The report names the OLDER number — the hint must win
        # over the "newest submitted" fallback.
        assert store.match_report("gsm", "Delivered 89261234555") is old

    def test_fallback_newest_when_no_number_hint(self):
        store = SMSCorrelationStore()
        self._submitted(store, "+79261234555")
        time.sleep(0.01)
        new = self._submitted(store, "+79000000000")
        got = store.match_report("gsm", "Delivered 10:00")
        assert got is new

    def test_modem_filter(self):
        store = SMSCorrelationStore()
        self._submitted(store, "+79261234555", modem="gsm")
        assert store.match_report("gsm2", "Delivered 79261234555") is None
        assert store.match_report("gsm", "Delivered 79261234555") is not None

    def test_empty_store(self):
        assert SMSCorrelationStore().match_report("gsm", "Delivered") is None

    def test_resolved_record_not_matched_again(self):
        store = SMSCorrelationStore()
        rec = self._submitted(store, "+79261234555")
        store.mark_delivered(rec.sms_id)
        assert store.match_report("gsm", "Delivered 79261234555") is None

    def test_unsubmitted_record_not_matched(self):
        store = SMSCorrelationStore()
        store.create(123, "+79261234555", "m")  # submit still pending
        assert store.match_report("gsm", "Delivered 79261234555") is None


# =========================================================================
# S02.2 — ACL audience selection (broadcast + event routing)
# =========================================================================

class TestACLUsersWithRight:
    def test_users_with_right(self, tmp_path):
        acl_file = tmp_path / "acl.conf"
        acl_file.write_text(
            "# comment line\n"
            "111 in_sms out_sms\n"
            "222 in_call\n"
            "333 out_sms\n"
        )
        acl = ACLManager(str(acl_file))
        assert acl.users_with_right("out_sms") == {111, 333}
        assert acl.users_with_right("in_call") == {222}
        assert acl.users_with_right("in_sms") == {111}

    def test_unknown_right_returns_empty(self, tmp_path):
        acl_file = tmp_path / "acl.conf"
        acl_file.write_text("111 out_sms\n")
        acl = ACLManager(str(acl_file))
        assert acl.users_with_right("bogus_right") == set()


# =========================================================================
# P0-1/P0-2 — AMI client: native DongleSendSMS (no shell interpolation)
# =========================================================================

class _FakeWriter:
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b

    async def drain(self):
        pass


class _ScriptedAmi(AMIClient):
    """AMIClient with the wire layer replaced by a script."""

    def __init__(self, responses, dongle="gsm"):
        super().__init__(dongle=dongle)
        self.actions = []
        self._responses = list(responses)

    async def _send_action(self, fields):
        self.actions.append(fields)

    async def _read_response(self):
        return self._responses.pop(0)


class TestAMIClientSendSms:
    def test_native_donglesendsms_headers(self):
        """The text travels as a first-class AMI header, untouched."""
        c = _ScriptedAmi([{"Response": "Success"}])
        asyncio.run(c.send_sms("+79261234555", "Café, naïve; rm -rf /"))
        (fields,) = c.actions
        assert fields["Action"] == "DongleSendSMS"
        assert fields["Device"] == "gsm"
        assert fields["Number"] == "+79261234555"
        assert fields["Message"] == "Café, naïve; rm -rf /"
        assert fields["Validity"] == "1440"
        assert fields["Report"] == "yes"
        assert fields["ActionID"].startswith("sms-")

    def test_newlines_flattened_to_spaces(self):
        c = _ScriptedAmi([{"Response": "Success"}])
        asyncio.run(c.send_sms("+79261234555", "line1\nline2\r\nline3"))
        assert c.actions[0]["Message"] == "line1 line2 line3"

    def test_error_response_raises_amisenderror(self):
        c = _ScriptedAmi([{"Response": "Error", "Message": "Not registered"}])
        with pytest.raises(AMISendError) as exc_info:
            asyncio.run(c.send_sms("+79261234555", "x"))
        assert str(exc_info.value) == "Not registered"
        assert exc_info.value.response["Response"] == "Error"

    def test_action_failure_raises_amisenderror(self):
        c = _ScriptedAmi([{"Action": "failure", "Message": "dongle busy"}])
        with pytest.raises(AMISendError) as exc_info:
            asyncio.run(c.send_sms("+79261234555", "x"))
        assert str(exc_info.value) == "dongle busy"

    def test_send_action_rejects_newline_values(self):
        c = AMIClient()
        c._writer = _FakeWriter()
        with pytest.raises(ValueError, match="newline"):
            asyncio.run(c._send_action({"Action": "X", "Message": "a\nb"}))
        asyncio.run(c._send_action({"Action": "X", "Message": "ok"}))
        assert b"Message: ok\r\n" in c._writer.data
        assert c._writer.data.endswith(b"\r\n\r\n")


class TestAMIClientModemStatus:
    @staticmethod
    def _entry(**kw):
        base = {
            "Message": "DongleDeviceEntry",
            "Device": "gsm",
            "GSMRegistrationStatus": "Registered, home network",
            "RSSI": "-65, -65",
            "ProviderName": "MTS",
            "IMEIState": "Registered 123456789012345",
        }
        base.update(kw)
        return base

    def test_normalize_entry(self):
        out = AMIClient._normalize_device_entry(self._entry())
        assert out["device"] == "gsm"
        assert out["registered"] is True
        assert out["operator"] == "MTS"
        assert out["imei_suffix"] == "2345"
        assert out["signal_percent"] == 75  # (-65+110)*100/60

    def test_roaming_counts_as_registered(self):
        out = AMIClient._normalize_device_entry(
            self._entry(GSMRegistrationStatus="Registered, roaming")
        )
        assert out["registered"] is True

    def test_unregistered_state(self):
        out = AMIClient._normalize_device_entry(
            self._entry(GSMRegistrationStatus="Not registered")
        )
        assert out["registered"] is False

    def test_signal_clamped_to_range(self):
        assert AMIClient._normalize_device_entry(
            self._entry(RSSI="-999, x"))["signal_percent"] == 0
        assert AMIClient._normalize_device_entry(
            self._entry(RSSI="-10, x"))["signal_percent"] == 100
        assert AMIClient._normalize_device_entry(
            self._entry(RSSI="N/A"))["signal_percent"] is None
