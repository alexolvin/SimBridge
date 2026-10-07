"""core.user_contacts — the per-user phonebook store.

Covers:
  - UserContactsStore: save/load round-trip, hot-reload by mtime,
    atomic replace, corrupt-file tolerance (keeps last good cache),
    add_unique (rejects a duplicate number or name, underscores names),
    delete_by_number / delete_by_name, resolve_number (normalized + raw),
    find_by_name tiering (exact > prefix > substring) and empty-query
    behavior.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from core.user_contacts import UserContactsStore


# ---------------------------------------------------------------------------
# UserContactsStore
# ---------------------------------------------------------------------------

def _store(tmp_path):
    return UserContactsStore(str(tmp_path / "user_contacts.json"))


class TestStoreRoundTrip:
    def test_missing_file_is_empty(self, tmp_path):
        s = _store(tmp_path)
        assert s.get(1) == []
        assert s.users() == []

    def test_save_load_round_trip(self, tmp_path):
        s = _store(tmp_path)
        n = s.replace_for(111, [
            {"name": "Ivanov", "number": "+79261234555"},
            {"name": "Petrow", "number": "+79262222222"},
        ])
        assert n == 2
        assert json.loads((tmp_path / "user_contacts.json").read_text()) == {
            "111": [
                {"name": "Ivanov", "number": "+79261234555"},
                {"name": "Petrow", "number": "+79262222222"},
            ]
        }
        # a fresh store instance sees the same data
        s2 = _store(tmp_path)
        assert s2.get(111) == s.get(111)
        assert s2.users() == [111]

    def test_replace_not_merge(self, tmp_path):
        s = _store(tmp_path)
        s.replace_for(111, [{"name": "Old", "number": "+79261111111"}])
        s.replace_for(111, [{"name": "New", "number": "+79262222222"}])
        assert s.get(111) == [{"name": "New", "number": "+79262222222"}]

    def test_replace_with_empty_clears_user(self, tmp_path):
        s = _store(tmp_path)
        s.replace_for(111, [{"name": "Old", "number": "+79261111111"}])
        n = s.replace_for(111, [])
        assert n == 0
        assert s.get(111) == []
        assert s.users() == []

    def test_entries_are_copied(self, tmp_path):
        # the API hands out copies — caller mutation must not corrupt
        s = _store(tmp_path)
        s.replace_for(111, [{"name": "Ivanov", "number": "+79261234555"}])
        e = s.get(111)[0]
        e["name"] = "MUTATED"
        assert s.get(111)[0]["name"] == "Ivanov"

    def test_invalid_entries_filtered_on_save(self, tmp_path):
        s = _store(tmp_path)
        n = s.replace_for(111, [
            {"name": "Good", "number": "+79261234555"},
            {"name": "", "number": "+79262222222"},
            {"name": "NoNum", "number": ""},
        ])
        assert n == 1
        assert s.get(111) == [{"name": "Good", "number": "+79261234555"}]


class TestStoreAddUnique:
    def test_add_new(self, tmp_path):
        s = _store(tmp_path)
        ok, reason, detail = s.add_unique(111, "Ivanov", "+79261111111")
        assert (ok, reason) == (True, "ok")
        assert s.get(111) == [{"name": "Ivanov", "number": "+79261111111"}]

    def test_number_normalized(self, tmp_path):
        s = _store(tmp_path)
        ok, reason, detail = s.add_unique(111, "Ivanov", "89261111111")
        assert (ok, detail) == (True, "+79261111111")
        assert s.get(111) == [{"name": "Ivanov", "number": "+79261111111"}]

    def test_short_internal_number_accepted(self, tmp_path):
        # a 4-digit internal extension is dialable (parse_destination),
        # even though it is not E.164
        s = _store(tmp_path)
        ok, reason, detail = s.add_unique(111, "Beeline", "0611")
        assert (ok, detail) == (True, "0611")
        assert s.get(111) == [{"name": "Beeline", "number": "0611"}]

    def test_short_service_number_accepted(self, tmp_path):
        # a 3-digit local service number is dialable too
        s = _store(tmp_path)
        ok, reason, detail = s.add_unique(111, "Operator", "100")
        assert (ok, detail) == (True, "100")
        assert s.get(111) == [{"name": "Operator", "number": "100"}]

    def test_short_number_duplicate_rejected(self, tmp_path):
        s = _store(tmp_path)
        s.add_unique(111, "Beeline", "0611")
        ok, reason, detail = s.add_unique(111, "Other", "0611")
        assert (ok, reason, detail) == (False, "number_exists", "Beeline")

    def test_name_spaces_to_underscores(self, tmp_path):
        s = _store(tmp_path)
        ok, reason, detail = s.add_unique(111, "Ivanov Ivan", "+79261111111")
        assert ok
        assert s.get(111) == [{"name": "Ivanov_Ivan", "number": "+79261111111"}]

    def test_duplicate_number_rejected(self, tmp_path):
        s = _store(tmp_path)
        s.add_unique(111, "Ivanov", "+79261111111")
        ok, reason, detail = s.add_unique(111, "Petrov", "+79261111111")
        assert (ok, reason, detail) == (False, "number_exists", "Ivanov")
        assert s.get(111) == [{"name": "Ivanov", "number": "+79261111111"}]

    def test_duplicate_number_normalized(self, tmp_path):
        s = _store(tmp_path)
        s.add_unique(111, "Ivanov", "+79261111111")
        ok, reason, _ = s.add_unique(111, "Petrov", "89261111111")
        assert (ok, reason) == (False, "number_exists")

    def test_duplicate_name_rejected(self, tmp_path):
        s = _store(tmp_path)
        s.add_unique(111, "Ivanov_Ivan", "+79261111111")
        ok, reason, detail = s.add_unique(111, "ivanov ivan", "+79262222222")
        assert (ok, reason, detail) == (False, "name_exists", "+79261111111")
        assert s.get(111) == [{"name": "Ivanov_Ivan", "number": "+79261111111"}]

    def test_no_name_rejected(self, tmp_path):
        s = _store(tmp_path)
        ok, reason, detail = s.add_unique(111, "   ", "+79261111111")
        assert (ok, reason, detail) == (False, "invalid", "no name")

    def test_bad_number_rejected(self, tmp_path):
        s = _store(tmp_path)
        ok, reason, detail = s.add_unique(111, "Ivanov", "not-a-number")
        assert (ok, reason, detail) == (False, "invalid", "bad number")

    def test_persists_to_disk(self, tmp_path):
        s = _store(tmp_path)
        s.add_unique(111, "Ivanov", "+79261111111")
        s2 = _store(tmp_path)
        assert s2.get(111) == [{"name": "Ivanov", "number": "+79261111111"}]


class TestStoreDelete:
    def test_delete_by_number_exact(self, tmp_path):
        s = _store(tmp_path)
        s.replace_for(111, [
            {"name": "Keep", "number": "+79261111111"},
            {"name": "Drop", "number": "+79262222222"},
        ])
        n = s.delete_by_number(111, "+79262222222")
        assert n == 1
        assert s.get(111) == [{"name": "Keep", "number": "+79261111111"}]

    def test_delete_by_number_normalized(self, tmp_path):
        s = _store(tmp_path)
        s.replace_for(111, [{"name": "Drop", "number": "+79262222222"}])
        assert s.delete_by_number(111, "89262222222") == 1
        assert s.get(111) == []

    def test_delete_by_number_removes_all_same_number(self, tmp_path):
        s = _store(tmp_path)
        s.replace_for(111, [
            {"name": "Dup One", "number": "+79262222222"},
            {"name": "Dup Two", "number": "+79262222222"},
            {"name": "Other", "number": "+79261111111"},
        ])
        assert s.delete_by_number(111, "+79262222222") == 2
        assert s.get(111) == [{"name": "Other", "number": "+79261111111"}]

    def test_delete_by_number_not_found(self, tmp_path):
        s = _store(tmp_path)
        s.replace_for(111, [{"name": "Keep", "number": "+79261111111"}])
        assert s.delete_by_number(111, "+79000000000") == 0
        assert s.get(111) == [{"name": "Keep", "number": "+79261111111"}]
        assert s.delete_by_number(111, "") == 0

    def test_delete_by_name_case_insensitive(self, tmp_path):
        s = _store(tmp_path)
        s.replace_for(111, [
            {"name": "Ivanov Ivan", "number": "+79261111111"},
            {"name": "Petrov", "number": "+79262222222"},
        ])
        assert s.delete_by_name(111, "ivanov IVAN") == 1
        assert s.get(111) == [{"name": "Petrov", "number": "+79262222222"}]

    def test_delete_by_name_exact_only(self, tmp_path):
        # a PREFIX of a name must not delete (destruction stays strict)
        s = _store(tmp_path)
        s.replace_for(111, [{"name": "Ivanov Ivan", "number": "+79261111111"}])
        assert s.delete_by_name(111, "Ivanov") == 0
        assert s.get(111) == [{"name": "Ivanov Ivan", "number": "+79261111111"}]

    def test_delete_persists_to_disk(self, tmp_path):
        s = _store(tmp_path)
        s.replace_for(111, [
            {"name": "Drop", "number": "+79261111111"},
            {"name": "Keep", "number": "+79262222222"},
        ])
        s.delete_by_number(111, "+79261111111")
        s2 = _store(tmp_path)  # fresh instance reads from disk
        assert s2.get(111) == [{"name": "Keep", "number": "+79262222222"}]

    def test_delete_other_user_unaffected(self, tmp_path):
        s = _store(tmp_path)
        s.replace_for(111, [{"name": "A", "number": "+79261111111"}])
        s.replace_for(222, [{"name": "A", "number": "+79261111111"}])
        s.delete_by_number(111, "+79261111111")
        assert s.get(222) == [{"name": "A", "number": "+79261111111"}]


class TestStoreHotReload:
    def test_external_write_picked_up(self, tmp_path):
        s = _store(tmp_path)
        s.replace_for(111, [{"name": "First", "number": "+79261111111"}])
        # an out-of-band writer (another process, hand edit)
        time.sleep(0.02)  # mtime granularity
        (tmp_path / "user_contacts.json").write_text(json.dumps({
            "222": [{"name": "Second", "number": "+79262222222"}],
        }))
        # advance mtime explicitly (some fs have coarse clocks)
        fut = time.time() + 2
        os.utime(tmp_path / "user_contacts.json", (fut, fut))
        assert s.get(111) == []
        assert s.get(222) == [{"name": "Second", "number": "+79262222222"}]

    def test_corrupt_file_keeps_last_good_cache(self, tmp_path):
        s = _store(tmp_path)
        s.replace_for(111, [{"name": "Good", "number": "+79261234555"}])
        (tmp_path / "user_contacts.json").write_text("{{{ not json")
        fut = time.time() + 2
        os.utime(tmp_path / "user_contacts.json", (fut, fut))
        assert s.get(111) == [{"name": "Good", "number": "+79261234555"}]

    def test_corrupt_root_shape_keeps_cache(self, tmp_path):
        s = _store(tmp_path)
        s.replace_for(111, [{"name": "Good", "number": "+79261234555"}])
        (tmp_path / "user_contacts.json").write_text("[1, 2, 3]")
        fut = time.time() + 2
        os.utime(tmp_path / "user_contacts.json", (fut, fut))
        assert s.get(111) == [{"name": "Good", "number": "+79261234555"}]


class TestResolveNumber:
    def test_normalized_and_raw_match(self, tmp_path):
        s = _store(tmp_path)
        s.replace_for(111, [{"name": "Ivanov", "number": "+79261234555"}])
        assert s.resolve_number(111, "+79261234555") == "Ivanov"
        assert s.resolve_number(111, "89261234555") == "Ivanov"  # normalized
        assert s.resolve_number(111, "79990000000") is None
        assert s.resolve_number(222, "+79261234555") is None  # per-user
        assert s.resolve_number(111, "") is None
        assert s.resolve_number(111, None) is None

    def test_raw_short_number_matched_verbatim(self, tmp_path):
        s = _store(tmp_path)
        s.replace_for(111, [{"name": "Service", "number": "900"}])
        assert s.resolve_number(111, "900") == "Service"


class TestFindByName:
    @pytest.fixture()
    def store(self, tmp_path):
        s = _store(tmp_path)
        s.replace_for(111, [
            {"name": "Ivanov Ivan", "number": "+79261111111"},
            {"name": "Ivanov Petr", "number": "+79262222222"},
            {"name": "Petrov", "number": "+79263333333"},
        ])
        return s

    def test_exact_match(self, store):
        assert [e["number"] for e in store.find_by_name(111, "ivanov ivan")] == \
            ["+79261111111"]
        assert [e["number"] for e in store.find_by_name(111, "Petrov")] == \
            ["+79263333333"]

    def test_prefix_tier_suppresses_substring(self, store):
        # "ivanov" prefixes two names — both come back, and the
        # substring-only "Petrov" does not sneak into this tier
        nums = [e["number"] for e in store.find_by_name(111, "ivanov")]
        assert nums == ["+79261111111", "+79262222222"]

    def test_substring_fallback(self, store):
        assert [e["number"] for e in store.find_by_name(111, "etro")] == \
            ["+79263333333"]

    def test_no_match(self, store):
        assert store.find_by_name(111, "nobody") == []

    def test_other_user_isolated(self, store):
        assert store.find_by_name(222, "ivanov") == []

    def test_empty_query(self, store):
        assert store.find_by_name(111, "") == []
        assert store.find_by_name(111, "   ") == []
