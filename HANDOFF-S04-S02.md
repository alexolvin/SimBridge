# HANDOFF — Stage 04 S04.2: Bridge Wiring to Asterisk

**Date:** 2026-08-11
**Status:** COMPLETE (with MANUAL_VERIFY items)

---

## What Was Implemented (This Session)

### S04.2 — Wire the bridge to Asterisk (single-node)

#### 1. PJSIP endpoint configuration
- `asterisk/pjsip.conf.example` — Complete tg-bridge endpoint:
  - `[tg-bridge]` endpoint: `direct_media=no`, `allow=ulaw,alaw`, `dtmf_mode=rfc2833`
  - `[tg-bridge-auth]` — auth credentials (placeholder: `simbridge-bridge-secret`)
  - `[tg-bridge-aor]` — single contact, 60s qualify
  - `[transport-udp]` — UDP transport, no SRTP (Tailscale encrypts)
  - Codec path documented: Telegram Opus 48kHz → sip-tg-bridge → SIP ulaw 8kHz → chan_dongle

#### 2. Dialplan — tg-bridge context
- `asterisk/extensions.conf.example` — New `[tg-bridge]` context for inbound bridge calls
  - Placeholder: routes to voicemail notification (S04.3 adds GSM dial logic)
  - `[incoming-mobile]` — updated comments for Stage 04 flow

#### 3. Call control state machine
- `core/call_control.py` — Deterministic state machine for call lifecycle:
  - `CallMachine` — per-call state with validated transitions
  - `CallRegistry` — thread-safe registry, modem reservation (atomic: one call at a time)
  - Incoming flow: `IDLE → RINGING → TELEGRAM_RINGING → ACCEPTED → BRIDGED → HANGUP → CLEANUP`
  - Outgoing flow: `IDLE → REQUESTED → MODEM_RESERVED → TELEGRAM_CALLING → ACCEPTED → GSM_DIALING → BRIDGED → HANGUP → CLEANUP`
  - Branches: reject, timeout-to-voicemail, caller-hangup

#### 4. Agent call control API
- `agent/routes.py` — Call control endpoints:
  - `POST /v1/call/incoming` — register incoming GSM call
  - `POST /v1/call/outgoing` — register outgoing call (blacklist check, modem reservation)
  - `POST /v1/call/{call_id}/accept` — accept call → bridge
  - `POST /v1/call/{call_id}/reject` — reject call → cleanup
  - `POST /v1/call/{call_id}/hangup` — hangup → cleanup
  - `GET /v1/call/{call_id}` — current call state
  - `GET /v1/calls` — list active calls

#### 5. Config updates
- `core/events.py` — Call event types: `CALL_INCOMING`, `CALL_OUTGOING`, `CALL_ACCEPTED`, `CALL_REJECTED`, `CALL_BRIDGED`, `CALL_HANGUP`, `CALL_TELEGRAM_RING`
- `agent/deps.py` — `get_call_registry()` accessor
- `agent/agent.py` — `CallRegistry` initialization in lifespan
- `scripts/generate_asterisk_config.py` — Bridge globals: `BRIDGE_ENDPOINT`, `BRIDGE_HOST`, `BRIDGE_PORT`, `OUTBOUND_RING_TIMEOUT`

#### 6. Tests
- `tests/test_call_control.py` — 36 new tests:
  - State machine (8): full flows, reject, voicemail, invalid transitions, serialization
  - Registry (9): create, modem reservation, cleanup, concurrent, counts
  - Config generator (1): bridge globals in output
  - Dialplan (2): tg-bridge context, routing extension
  - PJSIP config (6): endpoint, auth, AOR, direct_media, codecs, DTMF
  - Event types (4): call event enums

---

## What Was NOT Done (and Why)

| Item | Reason |
|---|---|
| TS04-3: `pjsip show endpoints` | MANUAL_VERIFY — requires running Asterisk |
| TS04-4: real two-way audio call | MANUAL_VERIFY — requires sip-tg-bridge build + credentials |
| Bridge ↔ Asterisk actual SIP registration | MANUAL_VERIFY — requires both components running |
| Codec negotiation verification | MANUAL_VERIFY — requires real call |
| S04.3: full call control with Telethon | Next sub-stage |
| S04.4: distributed mode | Final sub-stage |

---

## CONTROL Table — S04.2

| Check | Required | Result |
|---|---|---|
| Endpoint registers | `pjsip show endpoints` | MANUAL_VERIFY |
| Audio both directions | Real call | MANUAL_VERIFY |
| Single transcode | Codec path documented | UNIT TEST PASS (PJSIP config) |
| Asterisk keeps 5060 | Bridge on own port | UNIT TEST PASS (config) |
| direct_media=no | PJSIP config | UNIT TEST PASS |
| allow=ulaw,alaw | PJSIP config | UNIT TEST PASS |
| State machine valid | Full flows tested | UNIT TEST PASS (8 tests) |
| Modem reservation | Concurrent test | UNIT TEST PASS |
| Call API routes | 7 endpoints | UNIT TEST PASS |

---

## Test Results

```
132 passed, 6 skipped in 0.46s
```

Stages 01-03 tests (102) still pass. S04.2 adds 36 tests.

---

## ОТЧЁТ О ЧЕСТНОСТИ

**Что реально реализовано:**
- `core/call_control.py` — полный state machine с валидацией переходов, регистрация, резервирование модема
- `agent/routes.py` — 7 call control endpoints
- `agent/deps.py` — get_call_registry()
- `agent/agent.py` — CallRegistry initialization
- `core/events.py` — 7 call event types
- `asterisk/pjsip.conf.example` — полный endpoint с auth, transport, codecs
- `asterisk/extensions.conf.example` — [tg-bridge] контекст
- `scripts/generate_asterisk_config.py` — bridge globals
- `tests/test_call_control.py` — 36 тестов

**Что не реализовано:**
- Реальная регистрация bridge в Asterisk (MANUAL_VERIFY)
- Реальный двухсторонний звонок (MANUAL_VERIFY)
- Интеграция с Telethon для Telegram ring (S04.3)
- Сценарий: GSM caller → Telegram ring → accept → GSM answer → bridge (S04.3)

**Где использованы допущения:**
- Bridge password: `simbridge-bridge-secret` — placeholder, заменяется на deploy
- tg-bridge контекст в dialplan: пока маршрутизирует к voicemail-notify (S04.3 добавит GSM dial)
- Outgoing call: modem reservation блокирует второй звонок (atomic, один модем)

**Заглушки:**
- Нет заглушек в production-коде
- tg-bridge dialplan route — placeholder для voicemail notify (S04.3 заменит на GSM dial)

---

## For the Next Session (S04.3)

1. Incoming call: GSM → Telegram ring → accept → bridge GSM leg
2. Outgoing call: Telegram → GSM dial → bridge
3. Symmetric hangup: either side → both legs terminate
4. ACL check before call session
5. Tests: 4 incoming branches, 4 outgoing branches, orphan check, modem contention