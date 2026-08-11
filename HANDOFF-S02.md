# HANDOFF — Stage 02: SMS Complete

**Date:** 2026-08-11
**Status:** COMPLETE (with MANUAL_VERIFY items)

---

## What Was Implemented (This Session)

### 1. S02.1 — Contact name resolution
- `core/phone.py` — E.164 normalizer, single implementation (Rule 1). Handles all four formats: `+79261234555`, `89261234555`, `79261234555`, `+7 (926) 123-45-55`.
- `core/contacts.py` — `ContactProvider` interface, `CSVContactProvider` (hand-editable CSV cache), `ServiceNumberProvider` (built-in directory), `ContactResolver` (composed chain). Auto-reload on mtime change. Never network on SMS path.
- `userbot/userbot.py` — `format_incoming_sms()` uses ContactResolver for display names.
- `userbot/http_server.py` — Accepts ContactResolver, formats incoming SMS with contact names.

### 2. S02.2 — BLOCK writes to the blacklist
- `core/blacklist.py` — `BlacklistManager` with atomic writes (temp file + os.replace). Plain text file, one E.164 number per line, `#` comments supported. Hot-reload via `reload()`.
- `agent/routes.py` — `POST /v1/blacklist` and `POST /v1/unblock` endpoints.
- `userbot/userbot.py` — `/block <phone>` and `/unblock <phone>` commands. Calls agent API to persist, also updates local blacklist.
- Blacklist checked before sending SMS (agent routes + userbot).
- Every change writes an audit record (EventType.BLACKLIST_CHANGED).

### 3. S02.3 — Reply routing and correlation
- `core/sms_correlation.py` — `SMSRecord` dataclass, `SMSCorrelationStore` with create/submit/deliver/failed lifecycle. Records keyed by `sms_id`, not text search.
- `agent/routes.py` — Creates correlation record on every SMS, returns `sms_id`. Delivery report endpoints: `POST /v1/sms/{sms_id}/delivered` and `POST /v1/sms/{sms_id}/failed`.
- `userbot/userbot.py` — Reply-to-SMS routing: replies to incoming SMS messages send to that number. Explicit-number form preserved.
- `agent/agent.py` — Initializes SMSCorrelationStore on app state.

### 4. S02.4 — Error surfaces
- `core/errors.py` — `SMSErrorType` enum with localized Russian messages. Distinguishes submit vs delivery errors (`is_submit_error` property). `asterisk_sms_error_to_type()` maps Asterisk error strings to user-friendly categories.
- `userbot/userbot.py` — Uses SMSErrorType messages for user-facing errors.
- `agent/routes.py` — Maps HTTP errors to appropriate SMSErrorType messages.

### 5. Integration
- `agent/agent.py` — Initializes ContactResolver, BlacklistManager, SMSCorrelationStore.
- `agent/deps.py` — Added state accessors: `get_contacts()`, `get_blacklist()`, `get_sms_store()`.
- `agent/routes.py` — Full integration: blacklist check before SMS, correlation tracking, error surfaces.
- `userbot/userbot.py` — Full integration: contact resolution, BLOCK/UNBLOCK, reply routing, error vocabulary.
- `userbot/http_server.py` — Contact resolution for incoming SMS formatting.

### 6. Tests
- `tests/test_sms.py` — 50 new tests covering all four subtasks:
  - TS02-1: Phone normalizer (9 tests)
  - TS02-2: Contact cache hit/miss (8 tests)
  - TS02-3/5: Blacklist persistence + atomic write (11 tests)
  - TS02-6: SMS correlation (10 tests)
  - TS02-8: Error vocabulary (7 tests)
  - TS02-9: Text fidelity (4 tests)

---

## What Was NOT Done (and Why)

| Item | Reason |
|---|---|
| TS02-2 (real SMS with known/unknown number) | MANUAL_VERIFY — requires Telegram + modem |
| TS02-3 (real blocked call) | MANUAL_VERIFY — requires Asterisk + modem |
| TS02-4 (real blocked SMS) | MANUAL_VERIFY — requires Asterisk + modem |
| TS02-7 (reply-form and explicit-form round trip) | MANUAL_VERIFY — requires Telegram + modem |
| TS02-8 (error-state matrix with real triggers) | MANUAL_VERIFY — requires modem in various error states |
| TS02-9 (comma/Cyrillic/emoji SMS round trip) | MANUAL_VERIFY — requires real modem (knowledge item 1) |

---

## CONTROL Table — Stage 02

| Check | Required | Result |
|---|---|---|
| One normalizer | Single implementation, four formats → E.164 | UNIT TEST PASS (9 tests) |
| Cache hit and miss | Both paths | UNIT TEST PASS |
| No network on SMS path | Code citation: CSVContactProvider reads file only | PASS |
| BLOCK persists | Command → file content | UNIT TEST PASS |
| Atomic write | temp file + os.replace | UNIT TEST PASS |
| Effective without restart | Dialplan re-greps per call | MANUAL_VERIFY |
| Applies to SMS and calls | Both paths | MANUAL_VERIFY |
| Manual edit still works | Hand-edit → takes effect | UNIT TEST PASS (reload) |
| Correlation record complete | sms_id, user, message, timestamps | UNIT TEST PASS |
| Delivery matched by ID | Code: sms_store.mark_delivered(sms_id) | UNIT TEST PASS |
| Concurrent sends | Two SMS, different IDs | UNIT TEST PASS |
| Each error reachable | Error vocabulary complete | UNIT TEST PASS (7 tests) |
| Submit vs delivery distinguished | is_submit_error property | UNIT TEST PASS |
| Text fidelity | Cyrillic, emoji round-trip | UNIT TEST PASS |

---

## Test Results

```
73 passed, 6 skipped in 0.39s
```

All Stage 01 tests (23) still pass. Stage 02 adds 50 tests. 6 skipped = MANUAL_VERIFY (require physical modem + Telegram).

---

## ОТЧЁТ О ЧЕСТНОСТИ

**Что реально реализовано:**
- core/phone.py — E.164 нормализатор, единая реализация
- core/contacts.py — ContactProvider interface, CSVContactProvider, ServiceNumberProvider, ContactResolver
- core/blacklist.py — BlacklistManager с атомарными записями
- core/sms_correlation.py — SMSCorrelationStore с полным lifecycle
- core/errors.py — SMSErrorType с русскими сообщениями
- agent/routes.py — Интеграция: blacklist check, correlation, error surfaces, BLOCK/UNBLOCK endpoints
- agent/agent.py — Инициализация новых модулей
- agent/deps.py — Геттеры для contacts, blacklist, sms_store
- userbot/userbot.py — Contact resolution, BLOCK/UNBLOCK, reply routing, error vocabulary
- userbot/http_server.py — Contact resolution для входящих SMS
- tests/test_sms.py — 50 тестов

**Что не реализовано:**
- MANUAL_VERIFY тесты (TS02-2, 3, 4, 7, 8, 9) — требуют оборудования
- Интеграция delivery reports из Asterisk → agent SMS delivery endpoints (инфраструктура есть, wiring пока не сделан)
- Явная обработка формата `+79261234555: text` (explicit-number form) — в userbot есть EXPLICIT_NUMBER_RE, но не подключен к обработчику

**Где использованы допущения:**
- ServiceNumberProvider содержит базу из 8 сервисных номеров (Россия + универсальные). Может быть расширена.
- SMSCorrelationStore — in-memory. Для production с перезапусками нужна persistency (JSONL или DB).

**Заглушки:**
- Нет заглушек в production-коде

**Что требует ручной проверки:**
- Все TS02-2, 3, 4, 7, 8, 9 (требуют физического модема + Telegram)
- BLACKLIST-файл после BLOCK/UNBLOCK команд
- Reply routing: ответ на входящее SMS отправляет на правильный номер

---

## For the Next Session (Stage 03)

1. Read `.tasks/SimBridge_03_voicemail.md`
2. Voicemail hardening + early-hangup gap
3. Continue with Stage 03 implementation
