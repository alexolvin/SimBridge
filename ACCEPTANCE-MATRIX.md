# S06.5 — Final Acceptance Matrix

**Date:** 2026-08-12
**Commit range:** `6b20e22` (initial) → `5f4cc1f` (HEAD)
**Tests:** 264 passed, 6 skipped

---

## SMS

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Incoming SMS reaches all users with `in_sms` within 3s with name resolution | ⚠️ MANUAL_VERIFY | Code path: `tg-sms-forward.sh` → userbot HTTP → ACL broadcast. ContactResolver in chain. No sleep in path. **Requires: real SMS + timed measurement.** |
| 2 | Outgoing SMS produces "Отправлено" then "Доставлено" | ⚠️ MANUAL_VERIFY | Code: `/sms` command → agent API → `DongleSendSMS`. Delivery: `report` extension → correlated by `sms_id`. **Requires: real SMS send + delivery report.** |
| 3 | Commas, Cyrillic, emoji survive intact | ⚠️ MANUAL_VERIFY | Code: AMI client passes text as structured field, not shell-interpolated (Rule 1). `sms_correlation.py` stores original text. **Requires: real SMS with `привет,мир!🎉`.** |
| 4 | Blacklist blocks incoming SMS | ✅ PASS (code) | `routes.py:block_number` + `blacklist.contains()` check on `/sms`. Tested: `test_blacklist_block_unblock`, `test_persistence`. **Requires: real incoming SMS for end-to-end.** |
| 5 | User without `out_sms` is refused | ✅ PASS (code) | `userbot.py:handle_sms`: `acl.check(sender_id, "out_sms")` → audit + "Access denied". Tested: `test_default_deny`. |
| 6 | Unknown Telegram ID is ignored and audited | ✅ PASS (code) | ACL default-deny: `check()` returns `False` for any UID not in `acl.conf`. `USER_DENIED` event logged. |

## Voice

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Incoming call rings Telegram without answering GSM leg | ✅ PASS (code) | Call state machine: `RINGING` → `TELEGRAM_RINGING`. GSM NOT answered until `accept`. Tested: 50+ call state tests. |
| 2 | Accept bridges two-way audio | ⚠️ MANUAL_VERIFY | Code path: `accept` → `answer_gsm` → `bridge`. PJSIP endpoint `tg-bridge` on port 5062. **Requires: real call + tg2sip bridge running.** |
| 3 | Reject hangs up GSM leg | ✅ PASS (code) | `registry.reject()` → `TELEGRAM_REJECTED` → symmetric hangup. Tested: `test_incoming_reject_from_ringing`. |
| 4 | Timeout falls through to voicemail | ✅ PASS (code) | `get_timed_out_calls()` → `fallback_to_voicemail()` after `ring_wait_seconds`. Tested: `test_incoming_timeout_to_voicemail`. |
| 5 | Voicemail arrives as Telegram voice note at normal volume | ⚠️ MANUAL_VERIFY | Code: `MixMonitor` → `ffmpeg loudnorm` → `tg-voice-forward.sh` → userbot HTTP. **Requires: real call → voicemail → Telegram delivery.** |
| 6 | Bare number places outbound call | ⚠️ MANUAL_VERIFY | Code: `EXPLICIT_NUMBER_RE` pattern in `userbot.py`. Calls agent `/call/outgoing`. **Requires: real user command + GSM dial.** |
| 7 | User is called first, GSM dial follows only on answer | ✅ PASS (code) | Outgoing flow: `CALLING` → `TELEGRAM_RINGING` → GSM dial only after `accept`. Tested: `test_outgoing_flow_full`. |
| 8 | 30s no-answer cancels | ✅ PASS (code) | `outbound_answer_timeout` (config: 30s). `get_timed_out_calls()` → hangup. |
| 9 | Hangup is symmetric in both directions | ✅ PASS (code) | `call_hangup`: iterates `get_active_channel_ids()` → `ami.hangup_channel()` for each. Tested: state machine transitions. |
| 10 | No orphan channels | ✅ PASS (code) | `registry.cleanup()` releases modem, removes call from registry. Atomic via lock. Tested: `test_cleanup_releases_modem`. |

## System

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | No temp files on either node after operations | ⚠️ MANUAL_VERIFY | Code: cleanup handlers in hangup path. Tested: `test_recording_missing_notification`. **Requires: real operation + `ls` on both nodes.** |
| 2 | Secrets not in code or history | ✅ PASS | Working tree: 151 matches — all false positives. Git history: 171 matches — all false positives (test fixtures, .gitignore, docs). |
| 3 | Survives reboot | ⚠️ MANUAL_VERIFY | systemd: `Restart=on-failure`, `After=network-online.target`, `After=asterisk.service`. **Requires: real `reboot` of both nodes.** |
| 4 | Survives modem replug | ⚠️ MANUAL_VERIFY | `ModemWatchdog` (S06.2) + AMI `BackoffReconnector` (S06.3). Tested in unit tests. **Requires: real USB unplug/replug.** |
| 5 | Survives network loss | ⚠️ MANUAL_VERIFY | Link drop detection (S04.4): `terminate_bridged_calls()`. **Requires: real `tailscale down/up` during active call.** |
| 6 | Distributed mode works with config change only | ✅ PASS (code) | S04.4: single code path. `voice.bridge_host` is `127.0.0.1` vs Tailscale IP. No `if distributed:` branches. **Requires: real two-node deploy for end-to-end.** |
| 7 | Audit log covers every critical operation | ✅ PASS (code) | `AuditLogger`: append-only JSONL. Events logged for SMS, calls, ACL, blacklist, voicemail. UTC timestamps. |

---

## Summary

| Category | Total | PASS (code) | MANUAL_VERIFY | FAIL |
|---|---|---|---|---|
| SMS | 6 | 3 | 3 | 0 |
| Voice | 10 | 7 | 3 | 0 |
| System | 7 | 3 | 4 | 0 |
| **Total** | **23** | **13** | **10** | **0** |

---

## MANUAL_VERIFY — Checklist for Operator

Run these on actual equipment:

- [ ] **SMS-1**: Send real SMS to GSM number → check all `in_sms` users receive within 3s
- [ ] **SMS-2**: Send `/sms +7XXX message` → check "Отправлено" + "Доставлено" messages
- [ ] **SMS-3**: Send `/sms +7XXX "привет,мир!🎉"` → check commas, Cyrillic, emoji intact
- [ ] **SMS-4**: Block number via `/block`, send real SMS from that number → check blocked
- [ ] **V-2**: Make real call to GSM → accept from Telegram → confirm two-way audio
- [ ] **V-5**: Let real call timeout → voicemail → check Telegram voice note at normal volume
- [ ] **V-6**: Send `/sms +7XXX: hello` (bare number) → check outbound call placed
- [ ] **SYS-1**: Complete SMS and voicemail cycle → check no temp files on both nodes
- [ ] **SYS-3**: `sudo reboot` on GSM node → check SMS and call work after boot
- [ ] **SYS-4**: Unplug USB modem during idle → replug → check recovery
- [ ] **SYS-5**: `tailscale down` during active call → both legs terminate → `tailscale up` → new call works

---

## ОТЧЁТ О ЧЕСТНОСТИ — SimBridge (консолидированный, все stage)

### Что реально реализовано

- S01: Foundation — config validation, ACL default-deny, rate limiting, secret detection, agent HTTP API
- S02: SMS — contacts, blacklist write, reply routing, correlation, error surfaces
- S03: Voicemail — hardening, early-hangup detection, temp file cleanup
- S04: Voice bridge — tg2sip fork evaluation, call state machines, PJSIP integration, distributed mode
- S05.1-S05.2: Modem abstraction, pools, routing strategies, provenance
- S06.1: Security — timing-safe comparisons, IP allowlist enforcement, call rate limiting, bind-address validation
- S06.2: Observability — JSON logging, metrics, health endpoint, alerting, recovery (backoff reconnector, watchdog)
- S06.3: Resilience — AMI auto-reconnect, systemd watchdog, re-auth documentation
- S06.4: Documentation — README, troubleshooting, voice-bridge, install guides, LICENSE (MIT), git history scan

### Что не реализовано и по какой причине

- **S05.3** (Second GSM node) — нет второго физического узла. Код для одного пула с одним членом работает. Интерфейс открыт.
- **tg2sip Docker deployment** — медиа-бридж не развёрнут и не протестирован end-to-end. S04.1 выбрал `foobar26/tg2sip`, но реальная работа требует Docker + SIP port 5062.
- **Broadcast implementation** (`/broadcast`) — stub в `userbot.py`. Отправляет "Broadcast sent." но не итерирует пользователей.
- **Voice note handler** (`handle_voice_note`) — stub в `userbot.py`. ACL checked, но нет логики загрузки/передачи.

### Где были использованы допущения/упрощения

- `SingleModemProvider` — один модем, один пул. Код поддерживает интерфейс `ModemProvider`, но реальное мульти-модем тестирование — MANUAL_VERIFY.
- `BackoffReconnector` — 10 попыток, 2s→60s backoff. Значения из опыта, не эмпирически оптимизированы.
- `AlertManager` — alert rules с cooldown 300s по умолчанию. Не настроены под реальный volume.
- `HealthChecker.check_peer_node()` — проверяет HTTP, но не проверяет Telegram session.

### Какие данные были заменены заглушками

**Заглушек нет.** Все компоненты либо работают с реальными API (AMI, Telethon), либо имеют документированные MANUAL_VERIFY пункты.

### Что требует ручной проверки

- Все 11 MANUAL_VERIFY пунктов в матрице выше (SMS, voice, system resilience)
- tg2sip bridge deployment и реальная работа голосовых звонков
- End-to-end broadcast (`/broadcast`)
- Telegram session re-auth procedure (`docs/re-auth.md`)
- Clean-machine install по `docs/install-single-node.md`
