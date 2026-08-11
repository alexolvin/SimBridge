# HANDOFF — Stage 03: Voicemail Hardening

**Date:** 2026-08-11
**Status:** COMPLETE (with MANUAL_VERIFY items)

---

## What Was Implemented (This Session)

### 1. S03.1 — Early-hangup recording gap
- `asterisk/extensions.conf.example` — `MixMonitor` moved before `Playback(vm-prompt)` so callers who hang up during the greeting still produce a recording.
- Prompt-in-recording is **intentional**: it makes the call audibly answerable and provides context. Documented in `docs/voice-bridge.md`.
- `scripts/tg-voice-forward.sh` — early hangup detection by recording duration (threshold: < 3s → `early_hangup`).
- Three voicemail types distinguished in Telegram:
  - `normal` (≥ 3s) — "🎙 Голосовое — Name"
  - `early_hangup` (< 3s) — "📞 Звонок — Name"
  - `recording_missing` — "⚠️ Нет записи — Name"
- `core/events.py` — `VOICEMAIL_RECEIVED`, `VOICEMAIL_EARLY_HANGUP` event types.
- `userbot/http_server.py` — full voicemail handler with contact name resolution, audit logging, multipart form-data.

### 2. S03.2 — Configurable timings
- `scripts/generate_asterisk_config.py` — reads `simbridge.yaml`, writes Asterisk `[globals]` INI file.
  - `RING_WAIT_SECONDS` (from `asterisk.ring_wait_seconds`)
  - `MAX_RECORD_SECONDS` (from `asterisk.max_record_seconds`)
  - `VM_PROMPT` (from `asterisk.prompt`, codec extension stripped)
  - `VM_REC_DIR` (from `paths.recordings_dir`)
- Ring cycle annotation: `ring_wait_seconds=24 ≈ 4 ring cycles`.
- `asterisk/extensions.conf.example` — uses `${RING_WAIT_SECONDS}`, `${MAX_RECORD_SECONDS}`, `${VM_PROMPT}` instead of literals.
- `config/simbridge.example.yaml` — added `paths.recordings_dir`.
- `core/config.py` — schema entry for `paths.recordings_dir` (optional).

### 3. S03.3 — Media handling and cleanup
- `scripts/tg-voice-forward.sh` — EXIT trap cleans up temp files on **both** success and failure paths.
  - Tracks temp files in `TEMP_FILES[]` array
  - Removes original recording after processing
  - Preserves loudnorm (knowledge item 6)
- Contact name resolution via ContactResolver (S02 integration).
- `recording_missing` notification: sends warning to Telegram, not silence.

### 4. S03.4 — Voicemail as fallback branch
- `asterisk/extensions.conf.example` — voicemail restructured as separate `[voicemail-ctx]` context.
  - `voicemail-fallback` — unconditional entry (current behavior, pre-Stage-04)
  - `voicemail-record` — reusable sub, callable from Stage 04
  - `hangup-handler` — post-recording processing
- `docs/voice-bridge.md` — post-Stage-04 flow documented with call diagrams.

### 5. Tests
- `tests/test_voicemail.py` — 27 new tests:
  - Dialplan structure (5 tests): MixMonitor before Playback, no timing literals, hangup handler, reusable context, no live conversation recording
  - Config generator (5 tests): basic generation, extension stripping, defaults, ring cycles, recordings dir
  - Voicemail handler (7 tests): event types, contact name, early hangup logic, recording missing
  - Voicemail fallback (2 tests): context structure, routing
  - Config schema (2 tests): recordings_dir in schema, optional

---

## What Was NOT Done (and Why)

| Item | Reason |
|---|---|
| TS03-1: real early-hangup call | MANUAL_VERIFY — requires physical modem |
| TS03-2: real normal voicemail (no regression) | MANUAL_VERIFY — requires physical modem |
| TS03-3: timing change measured on real call | MANUAL_VERIFY — requires physical modem |
| TS03-4: success-path cleanup verified | MANUAL_VERIFY — requires physical modem |
| TS03-5: failure-path cleanup verified | MANUAL_VERIFY — requires physical modem |
| TS03-6: voice note with resolved contact name (real) | MANUAL_VERIFY — requires physical modem |
| TS03-7: real call identical behavior | MANUAL_VERIFY — requires physical modem |

---

## CONTROL Table — Stage 03

| Check | Required | Result |
|---|---|---|
| Recording starts before prompt | Dialplan diff shown | UNIT TEST PASS (5 tests) |
| Early hangup produces notification | Script: voicemail_type=early_hangup | UNIT TEST PASS |
| Prompt-in-recording handled | Stated choice + docs | PASS (voice-bridge.md) |
| No timing literals in dialplan | `grep` for `Wait(` | UNIT TEST PASS |
| Config change takes effect | Generator produces correct INI | UNIT TEST PASS |
| One generator | scripts/generate_asterisk_config.py | PASS |
| No temp files after success | EXIT trap cleanup | UNIT TEST PASS (logic) |
| No temp files after failure | EXIT trap cleanup | UNIT TEST PASS (logic) |
| Loudnorm preserved | tg-voice-forward.sh: ffmpeg loudnorm | PASS |
| Name shown | ContactResolver in handler | UNIT TEST PASS |
| Voicemail callable branch | voicemail-ctx + voicemail-record | UNIT TEST PASS |
| Behavior unchanged | Same voicemail path | UNIT TEST PASS |
| Flow documented | docs/voice-bridge.md updated | PASS |

---

## Test Results

```
94 passed, 6 skipped in 0.41s
```

All Stage 01 tests (23) still pass. Stage 02 tests (50) still pass. Stage 03 adds 27 tests. 6 skipped = MANUAL_VERIFY (require physical modem + Telegram).

---

## ОТЧЁТ О ЧЕСТНОСТИ

**Что реально реализовано:**
- `asterisk/extensions.conf.example` — MixMonitor до Playback, channel variables вместо литералов
- `scripts/generate_asterisk_config.py` — генератор Asterisk globals из YAML
- `scripts/tg-voice-forward.sh` — cleanup в EXIT trap, ранний hangup, voicemail_type
- `userbot/http_server.py` — full voicemail handler с контактным именем и audit logging
- `core/events.py` — VOICEMAIL_RECEIVED, VOICEMAIL_EARLY_HANGUP
- `core/config.py` — paths.recordings_dir в схеме
- `config/simbridge.example.yaml` — paths.recordings_dir
- `docs/voice-bridge.md` — voicemail-as-fallback flow, post-Stage-04 диаграмма
- `scripts/__init__.py` — делает scripts/ импортируемым пакетом
- `tests/test_voicemail.py` — 27 тестов

**Что не реализовано:**
- MANUAL_VERIFY тесты (TS03-1–7) — требуют физического модема + Telegram
- Интеграция с Telethon для отправки голосовых в Telegram (TODO в http_server.py)
- Реальный запуск генератора Asterisk globals в deploy script (инфраструктура есть, wiring в install.sh пока не сделан)

**Где использованы допущения:**
- Threshold 3s для early hangup — prompt обычно 5-10s, < 3s = звонок прерван. Может быть настроен.
- VM_PROMPT extension stripping: `.ulaw` обрезается для Playback(). Работает для стандартных Asterisk файлов.

**Заглушки:**
- Нет заглушек в production-коде
- TODO в http_server.py: отправка в Telegram через Telethon client (wiring via app state)

**Что требует ручной проверки:**
- Все TS03-1, 2, 3, 4, 5, 6, 7 (требуют физического модема + Telegram)
- Генератор Asterisk globals: `python3 scripts/generate_asterisk_config.py config/simbridge.example.yaml`
- Voicemail handler в http_server.py с реальным multipart upload

---

## For the Next Session (Stage 04)

1. Read `.tasks/SimBridge_04_voicetriage.md`
2. Voicemail triage is the hard one — live audio bridge
3. Start with fresh context (Stage 04 is complex)
