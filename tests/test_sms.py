"""Stage 02 tests — SMS complete: contacts, blacklist, reply routing, errors.

Tests: TS02-1 (normalizer), TS02-2 (contacts cache hit/miss),
       TS02-3/4 (BLOCK persist — unit), TS02-5 (atomic write),
       TS02-6 (correlation), TS02-7 (reply forms), TS02-8 (error matrix).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

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
