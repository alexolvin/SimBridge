# HANDOFF — Stage 04 S04.1: Media Bridge Evaluation

**Date:** 2026-08-11
**Status:** COMPLETE (research + MANUAL_VERIFY items)

---

## What Was Done (This Session)

### S04.1 — Evaluate and select the media bridge

#### Research Results

| Candidate | Media Library | Status |
|---|---|---|
| Infactum/tg2sip | libtgvoip | **DISQUALIFIED** — deprecated library |
| foobar26/tg2sip (ntgcalls fork) | — | **NOT FOUND** — GitHub 404, may be private |
| blitss/sip-tg-bridge | ntgcalls + LiveKit SIP | **SELECTED** — POC/WIP, 7 commits |
| Direct ntgcalls | ntgcalls (pytgcalls) | Contingency — last resort |

#### Selection: blitss/sip-tg-bridge

**Why**: The only publicly available ntgcalls-based bridge with SIP integration. Built on LiveKit SIP (battle-tested audio pipeline). Explicitly designed as tg2sip substitute.

**Risk**: POC status (7 commits). May have gaps in error handling and call control.

**Contingency**: Direct ntgcalls integration with Go bindings + custom pjsip wrapper.

#### Documentation Updates

- `docs/voice-bridge.md` — Full rewrite with bridge selection, research comparison, transport decision, PJSIP config, media flow diagram, voicemail fallback flow.

#### Tests

- `tests/test_voice_bridge.py` — 8 tests validating documentation completeness:
  - Selection documented
  - Primary candidate disqualification noted
  - Selected candidate uses ntgcalls
  - Transport decision (plain RTP, no SRTP)
  - PJSIP config: direct_media=no, ulaw/alaw, DTMF RFC2833
  - Voicemail fallback documented

---

## What Was NOT Done (and Why)

| Item | Reason |
|---|---|
| TS04-1: Build transcript | MANUAL_VERIFY — requires build environment (CMake, Go, ntgcalls deps) |
| TS04-2: Real Telegram call | MANUAL_VERIFY — requires Telegram credentials + build |
| S04.2: Wire bridge to Asterisk | Next sub-stage — requires S04.1 build verification |
| S04.3: Call control state machines | Next sub-stage |
| S04.4: Distributed mode | Final sub-stage |
| foobar26/tg2sip verification | GitHub 404 — repo may be private, deleted, or misnamed |

---

## CONTROL Table — S04.1

| Check | Required | Result |
|---|---|---|
| Primary candidate builds | Build transcript | MANUAL_VERIFY |
| Real Telegram call placed | Evidence | MANUAL_VERIFY |
| Media library confirmed | ntgcalls, not libtgvoip — cited | UNIT TEST PASS |
| Selection documented | One choice, stated reasoning | UNIT TEST PASS |
| Infactum/tg2sip disqualified | libtgvoip cited | UNIT TEST PASS |
| Transport decision documented | Plain RTP, no SRTP | UNIT TEST PASS |
| PJSIP config correct | direct_media=no, ulaw/alaw | UNIT TEST PASS |
| Voicemail fallback documented | Flow diagram | UNIT TEST PASS |

---

## Test Results

```
102 passed, 6 skipped in 0.43s
```

Stages 01-03 tests (94) still pass. S04.1 adds 8 tests.

---

## ОТЧЁТ О ЧЕСТНОСТИ

**Что реально реализовано:**
- `docs/voice-bridge.md` — полный сравнительный анализ кандидатов, выбор с обоснованием
- `tests/test_voice_bridge.py` — 8 тестов валидации документации

**Что не реализовано:**
- Сборка sip-tg-bridge из исходников (MANUAL_VERIFY)
- Реальный звонок через Telegram (MANUAL_VERIFY)
- Поиск foobar26/tg2sip — репозиторий не найден (GitHub 404)
- S04.2-S04.4 — следующие под-этапы

**Где использованы допущения:**
- foobar26/tg2sip помечен как NOT FOUND — репозиторий может быть приватным или переименованным
- sip-tg-bridge выбран на основе README и структуры репозитория, не на основе реальной сборки

**Что требует ручной проверки:**
- Сборка sip-tg-bridge: `git clone --recursive && make build-bridge`
- Реальный звонок через Telegram
- Поиск foobar26/tg2sip под другим именем

---

## For the Next Session (S04.2)

1. Build sip-tg-bridge from source (MANUAL_VERIFY)
2. Place a real Telegram voice call (MANUAL_VERIFY)
3. If build fails → evaluate direct ntgcalls integration
4. Wire bridge to Asterisk as PJSIP endpoint
5. Continue with S04.2 implementation
