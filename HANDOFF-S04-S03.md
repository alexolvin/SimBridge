# HANDOFF — Stage 04 S04.3: Call Control State Machines

**Date:** 2026-08-11
**Status:** COMPLETE (with MANUAL_VERIFY items)

---

## What Was Implemented (This Session)

### S04.3 — Call control: state machines and both directions

#### 1. Expanded call state machine
- `core/call_control.py` — Granular S04.3 states:
  - **Incoming**: `IDLE → RINGING → TELEGRAM_RINGING → TELEGRAM_ACCEPTED → GSM_ANSWERED → BRIDGED → HANGUP → CLEANUP`
    - Branches from RINGING: GSM hangup → HANGUP, Telegram reject → REJECTED, timeout → VOICEMAIL
    - Branches from TELEGRAM_RINGING: same four branches
  - **Outgoing**: `IDLE → REQUESTED → ACL_CHECKED → MODEM_RESERVED → TELEGRAM_CALLING → USER_ACCEPTED → GSM_DIALING → GSM_RINGING → CONNECTED → BRIDGED → HANGUP → CLEANUP`
    - Branches: ACL denied → ACL_DENIED, Telegram timeout → TELEGRAM_TIMEOUT, GSM busy → GSM_BUSY, GSM no answer → GSM_NO_ANSWER, GSM error → GSM_ERROR
  - New terminal states: `ACL_DENIED`, `TELEGRAM_TIMEOUT`, `GSM_BUSY`, `GSM_NO_ANSWER`, `GSM_ERROR`
  - `_TERMINAL_STATES` set — no further transitions from terminal states
  - `CallMachine.is_terminal` property
  - `CallMachine.check_duration_exceeded(max_seconds)` — duration limit check
  - `CallMachine.get_active_channel_ids()` — orphan channel detection

#### 2. Bridge leg tracking
- `CallMachine.gsm_channel_id` — Asterisk channel name for the GSM leg
- `CallMachine.bridge_channel_id` — Asterisk channel name for the PJSIP bridge leg
- `CallMachine.telegram_user_id` — Telegram user for the call
- `CallMachine.telegram_call_id` — Telegram call session ID
- `registry.set_bridge_leg()` — record bridge channel
- `registry.set_telegram_call_id()` — record Telegram session

#### 3. Higher-level orchestration methods
- `registry.start_telegram_ring()` — RINGING → TELEGRAM_RINGING
- `registry.accept_incoming()` — TELEGRAM_RINGING → TELEGRAM_ACCEPTED
- `registry.answer_gsm()` — TELEGRAM_ACCEPTED → GSM_ANSWERED
- `registry.bridge_call()` — both directions → BRIDGED
- `registry.hangup()` — symmetric hangup with reason recording
- `registry.reject()` — reject with reason recording
- `registry.fallback_to_voicemail()` — timeout → VOICEMAIL
- `registry.start_telegram_calling()` — MODEM_RESERVED → TELEGRAM_CALLING
- `registry.user_accepted()` — TELEGRAM_CALLING → USER_ACCEPTED
- `registry.dial_gsm()` — USER_ACCEPTED → GSM_DIALING
- `registry.gsm_ringing()` — GSM_DIALING → GSM_RINGING
- `registry.gsm_connected()` — GSM_RINGING → CONNECTED
- `registry.gsm_busy()` — GSM_DIALING → GSM_BUSY
- `registry.gsm_no_answer()` — GSM_RINGING → GSM_NO_ANSWER
- `registry.gsm_error()` — GSM_DIALING → GSM_ERROR
- `registry.telegram_timeout()` — TELEGRAM_CALLING → TELEGRAM_TIMEOUT

#### 4. Timeout and orphan detection
- `registry.get_timed_out_calls(ring_wait_seconds, max_call_seconds)` — detects ringing calls exceeding ring timeout and bridged calls exceeding max duration
- `registry.get_orphan_channel_ids()` — returns all channel IDs for active calls

#### 5. ACL check before outgoing calls (GPT §26)
- `core/acl.py` — pre-existing ACL manager with per-right checks (`out_call`, `in_call`)
- `agent/routes.py` — `call_outgoing()` checks `acl.check(uid, "out_call")` before any call session
- `agent/deps.py` — `get_acl()` dependency
- `agent/agent.py` — ACL initialization from `telegram.acl_file`
- New event: `CALL_ACL_CHECK` with outcome `allowed`/`denied`

#### 6. Agent API — S04.3 endpoints
- `POST /v1/call/{call_id}/telegram-ring` — start Telegram ringing (RINGING → TELEGRAM_RINGING)
- `POST /v1/call/{call_id}/set-gsm-channel` — record GSM channel ID
- `POST /v1/call/{call_id}/answer-gsm` — answer GSM leg (TELEGRAM_ACCEPTED → GSM_ANSWERED)
- `POST /v1/call/{call_id}/bridge` — mark both legs bridged
- `POST /v1/call/{call_id}/set-bridge-leg` — record bridge channel ID
- `POST /v1/call/check-timeouts` — detect and handle ring timeout / duration exceeded
- Updated `call_outgoing` — ACL check before modem reservation
- Updated `call_accept` — transition to TELEGRAM_ACCEPTED (not ACCEPTED)
- Updated `call_reject` — audit with reason
- Updated `call_hangup` — symmetric hangup: terminates both legs via AMI before cleanup

#### 7. AMI client — call control actions
- `originate_call()` — initiate outbound call to any endpoint
- `hangup_channel()` — hang up a specific channel
- `answer_channel()` — answer a ringing channel
- `list_channels()` — CoreShowChannels for orphan detection
- `set_channel_variable()` — set dialplan variables (e.g., TG_ACCEPTED=1)

#### 8. Dialplan — S04.3 flow
- `asterisk/extensions.conf.example` — Updated `[incoming-mobile]`:
  - Do NOT answer GSM channel — caller hears real ringback
  - Notify agent via AGI (`notify-agent-agi.py`)
  - Wait loop checking `TG_ACCEPTED` variable (set by agent via AMI)
  - On timeout: hangup, falls through to voicemail
  - On accept: answer GSM, originate bridge leg to tg-bridge
- New `[tg-bridge-sip]` context — outgoing GSM dial from tg-bridge
  - Dials GSM via `Dongle/${GSM_TARGET}`
  - Handles BUSY, NOANSWER, ANSWERED outcomes
  - Notifies agent via AGI for each outcome

#### 9. Config generator — call duration globals
- `scripts/generate_asterisk_config.py` — new `MAX_CALL_SECONDS` global

#### 10. Event types — S04.3
- New events: `CALL_ACL_CHECK`, `CALL_GSM_ANSWERED`, `CALL_GSM_DIALED`, `CALL_GSM_RINGING`, `CALL_GSM_CONNECTED`, `CALL_TELEGRAM_TIMEOUT`, `CALL_DURATION_EXPIRED`

#### 11. Tests
- `tests/test_call_control.py` — complete rewrite with 74 tests:
  - State machine (23): full flows, all incoming branches, all outgoing branches, invalid transitions, terminal states, serialization, duration check
  - Registry (25): create, orchestration methods, bridge leg tracking, timeout checking, orphan detection, counts
  - ACL (5): allowed, denied, unknown user, user count, per-right checks
  - Config generator (1): bridge + call duration globals
  - Dialplan (6): tg-bridge context, tg-bridge-sip context, Telegram flow, GSM dial
  - PJSIP config (6): endpoint, auth, AOR, direct_media, codecs, DTMF
  - Event types (8): incoming, outgoing, accepted, bridged, ACL check, GSM answered, Telegram timeout, duration expired

---

## What Was NOT Done (and Why)

| Item | Reason |
|---|---|
| TS04-5: 4 real incoming call branches | MANUAL_VERIFY — requires running Asterisk + sip-tg-bridge |
| TS04-6: 4 real outgoing call branches | MANUAL_VERIFY — requires real GSM modem + Telegram call |
| TS04-7: orphan channel check on real hardware | MANUAL_VERIFY — requires live call |
| TS04-8: concurrent modem contention on real hardware | MANUAL_VERIFY — requires simultaneous calls |
| AGI script `notify-agent-agi.py` | Not yet created — S04.3 dialplan references it |
| Telethon integration for Telegram ring/accept | Not yet created — routes call registry methods, but Telethon userbot not wired |
| S04.4: distributed mode | Final sub-stage |

---

## CONTROL Table — S04.3

| Check | Required | Result |
|---|---|---|
| Ringback before answer | Real incoming call | MANUAL_VERIFY |
| All 4 incoming branches | accept / reject / timeout / hangup | UNIT TEST PASS (4 tests) |
| All 4 outgoing branches | answered / timeout / busy / error | UNIT TEST PASS (4 tests) |
| Symmetric hangup | Both legs terminated | CODE: AMI hangup_channel in call_hangup route |
| Atomic reservation | Two concurrent, one winner | UNIT TEST PASS (modem_busy test) |
| ACL before session | Code citation | CODE: acl.check() in call_outgoing() before create_outgoing() |
| No orphan channels | `core show channels` empty | CODE: get_orphan_channel_ids() + cleanup() |
| Terminal states | 9 terminal states | UNIT TEST PASS (is_terminal test) |
| Bridge leg tracking | gsm_channel_id + bridge_channel_id | UNIT TEST PASS (2 tests) |
| Duration exceeded | check_duration_exceeded() | UNIT TEST PASS (2 tests) |

---

## Test Results

```
177 passed, 6 skipped in 0.55s
```

Stages 01-03 tests (102) still pass. S04.3 adds 74 tests, replaces 36 from S04.2.

---

## ОТЧЁТ О ЧЕСТНОСТИ

**Что реально реализовано:**
- `core/call_control.py` — полный state machine с S04.3 гранулярностью, 9 термінальных состояний, bridge leg tracking, timeout checking, orphan detection
- `core/events.py` — 7 новых event types для S04.3
- `agent/routes.py` — 7 новых endpoints, обновлены accept/reject/hangup с S04.3 логикой
- `agent/ami_client.py` — originate/hangup/answer/list/set-variable
- `agent/deps.py` — get_acl()
- `agent/agent.py` — ACL initialization
- `asterisk/extensions.conf.example` — S04.3 dialplan с Telegram ring flow + [tg-bridge-sip]
- `scripts/generate_asterisk_config.py` — MAX_CALL_SECONDS
- `tests/test_call_control.py` — 74 теста, полный охват state machine + registry + ACL + dialplan

**Что не реализовано:**
- Реальная регистрация bridge в Asterisk (MANUAL_VERIFY)
- Реальный двухсторонний звонок (MANUAL_VERIFY)
- Интеграция с Telethon для Telegram ring/accept (только registry methods, не Telethon)
- AGI скрипт notify-agent-agi.py (ссылается из dialplan, не создан)
- S04.4: distributed mode

**Где использованы допущения:**
- ACL файл читается из `telegram.acl_file` тот же файл, что и для Telegram ACL
- AGI notify-agent-agi.py — placeholder в dialplan, создаётся отдельно
- AMI originate_call() — использует стандартный AMI Originate с параметрами
- AMI answer_channel() — использует Command: channel answer (chan_dongle compat)

**Заглушки:**
- Нет заглушек в production-коде
- `notify-agent-agi.py` в dialplan — placeholder для AGI-интеграции

---

## For the Next Session (S04.4)

1. Change `voice.bridge_host` from `127.0.0.1` to Tailscale IP
2. Measure one-way latency and jitter on real cross-node call
3. Handle inter-node failure (tailscale down mid-call)
4. PJSIP local_net and NAT settings for tailnet
5. Tests: TS04-9 (cross-node call), TS04-10 (mid-call link drop)
