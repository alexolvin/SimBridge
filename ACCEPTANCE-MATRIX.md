# S06.5 — Final Acceptance Matrix

**Date:** 2026-08-15 (rewritten — see status legend)
**Head:** `5c6a5c9`
**Tests:** 269 passed, 6 skipped (unit tests only — see Rule 3)

> **Superseded by verification.** The 2026-08-12 version of this matrix
> marked items "PASS (code)" that were never executed end to end, and claimed
> a git-history secret scan that had not been run. This version restates every
> criterion with a status backed by evidence as of the 2026-08-14/15 audit
> (`.handoff/HANDOFF-VERIFY-S01-20260814.md`). It will be regenerated with
> fresh evidence when Stage 06 executes.

**Status legend**

- `VERIFIED` — real run, artifact shown
- `CODE+UNITS` — code path exists and unit tests pass; no end-to-end run
- `NOT-VERIFIED` — claimed by code, no run of any kind
- `NOT-IMPLEMENTED` — missing or dead code path
- `BLOCKED` — requires external action (real device, second node, operator)

---

## SMS

| # | Criterion | Status | Evidence / gap |
|---|---|---|---|
| 1 | Incoming SMS reaches all `in_sms` users within 3s with name resolution | NOT-VERIFIED | New stack never deployed: production runs the pre-refactor dialplan (`SMS_NAME=unknown`). Code + units exist; no timed real run. |
| 2 | Outgoing SMS produces "Отправлено" then "Доставлено" | NOT-VERIFIED | No real send through the new stack. Delivery reports are **not** correlated by `sms_id` (dialplan sends marker text; `SMSCorrelationStore` is in-memory). |
| 3 | Commas, Cyrillic, emoji survive intact | NOT-VERIFIED | AMI `send_sms` interpolates text into a CLI command (`DongleSendSMS(gsm,to,'{text}')`) — an apostrophe in text breaks the SMS; the "URL-encoding" docstring is false. |
| 4 | Blacklist blocks incoming SMS | NOT-VERIFIED | `core/blacklist.py` is correct (atomic writes, hot-reload, unit tests). The new example dialplan has **no blacklist check on the incoming SMS path** (S02.2 requires SMS + calls). Production uses the old grep-based check. |
| 5 | User without `out_sms` is refused | CODE+UNITS | `userbot.py:handle_sms` → `acl.check(sender_id, "out_sms")` → audit + deny. Unit: `test_default_deny`. No real non-ACL account attempt yet. |
| 6 | Unknown Telegram ID is ignored and audited | CODE+UNITS | ACL default-deny in `core/acl.py` (unit tested). Production audit file does not exist — no event has ever been logged in production. |

## Voice

The voice bridge was **never assembled**. `docs/voice-bridge.md` marks
`MANUAL_VERIFY`. Production `pjsip.conf` has no `tg-bridge` endpoint.
State-machine code exists but is disconnected: `TG_ACCEPTED` is never set,
`/call/check-timeouts` has no poller, there is no accept → answer-gsm →
bridge orchestrator, no outgoing-call / accept / reject / RING handlers in
the userbot, and transitions do not write audit records (S04.3).

| # | Criterion | Status | Evidence / gap |
|---|---|---|---|
| 1 | Incoming call rings Telegram without answering GSM leg | NOT-VERIFIED | No RING notification path in userbot. Units pass on the isolated state machine only. |
| 2 | Accept bridges two-way audio | NOT-IMPLEMENTED | No tg-bridge process deployed anywhere; no PJSIP endpoint in production. |
| 3 | Reject hangs up GSM leg | NOT-VERIFIED | No reject handler in userbot. Unit `test_incoming_reject_from_ringing` exercises the registry only. |
| 4 | Timeout falls through to voicemail | NOT-VERIFIED | `get_timed_out_calls()` exists; **no background task calls it**. Example dialplan hangs up on timeout instead of falling to voicemail (comment contradicts code). |
| 5 | Voicemail arrives as Telegram voice note at normal volume | NOT-VERIFIED | Old production path works (legacy `tg-voice-forward.sh`); new-stack path never run. `MixMonitor`-before-prompt fix (S03.1) not deployed. |
| 6 | Bare number places outbound call | NOT-IMPLEMENTED | No bare-number → call-request handler in userbot. |
| 7 | User is called first, GSM dial follows only on answer | NOT-VERIFIED | Outgoing flow unimplemented at handler level; state machine units only. |
| 8 | 30s no-answer cancels | NOT-VERIFIED | No timeout poller (same as #4). |
| 9 | Hangup is symmetric in both directions | NOT-VERIFIED | Code exists; never exercised against real Asterisk. |
| 10 | No orphan channels | NOT-VERIFIED | `registry.cleanup()` unit tested; no real-call evidence. |

## System

| # | Criterion | Status | Evidence / gap |
|---|---|---|---|
| 1 | No temp files on either node after operations | NOT-VERIFIED | No real operation on the new stack to observe. |
| 2 | Secrets not in code or history | PARTIAL | Working tree: scanned 2026-08-12, 151 matches — all false positives (documented). **Git history scan NOT RUN** (the 2026-08-12 version falsely claimed it was). Due: TS06-11. |
| 3 | Survives reboot | NOT-VERIFIED | systemd units have `Restart=on-failure`; no real reboot test. |
| 4 | Survives modem replug | NOT-VERIFIED | `ModemWatchdog` is **never instantiated** (grep: 0 outside tests). AMI `BackoffReconnector.start()` never called. |
| 5 | Survives network loss | NOT-VERIFIED | `terminate_bridged_calls()` exists; no `tailscale down/up` test run. |
| 6 | Distributed mode works with config change only | CODE+UNITS | Single code path — `voice.bridge_host` is the only knob (verified by code read). Never deployed distributed. |
| 7 | Audit log covers every critical operation | CODE+UNITS | `AuditLogger` is append-only JSONL, UTC ISO-8601 (unit tested). Production has no audit file — nothing has been logged. Call transitions are not wired to the audit logger. |

---

## Summary

| Category | Total | VERIFIED | CODE+UNITS | NOT-VERIFIED | NOT-IMPLEMENTED |
|---|---|---|---|---|---|
| SMS | 6 | 0 | 2 | 4 | 0 |
| Voice | 10 | 0 | 0 | 7 | 3 |
| System | 7 | 0 | 2 | 4 | 1* |
| **Total** | **23** | **0** | **4** | **15** | **4** |

\* counts the voice-adjacent watchdog item.

Nothing in the new stack has a full end-to-end artifact yet. That is the
point of the current work plan: P0 fixes → fresh-install acceptance on
3p14-aaa + vzu5-claw → real-device SMS → voice. Each item above is
re-evaluated with evidence as its stage completes; this file is regenerated
at S06.5.

---

## What is verified working (evidence-backed, 2026-08-14/15)

- Unit suite: 269 passed, 6 skipped (`python3 -m pytest tests/ -q`)
- Config validation: unknown key → error, missing key → error naming the key,
  missing secret env → refuses to start, `0.0.0.0` listen rejected,
  effective config logged with secrets redacted (startup log in journal)
- `core/audit.py`: append-only JSONL, UTC ISO-8601 (unit tests)
- `core/phone.py`: E.164, all four input formats (unit tests)
- `core/blacklist.py`: atomic temp+rename writes, hot-reload (unit tests)
- `core/acl.py`: default-deny, hot-reload (unit tests)
- `simbridge-agent` API: starts, binds Tailscale IP only, bearer token
  (timing-safe) + IP allowlist + replay window (journal + unit tests).
  NOTE: production instance was in a watchdog crash-loop (P0-1) until the
  2026-08-15 unit fix, and 403'd all peer traffic due to hostname
  allowlist entries (P0-2) until the normalization fix + IP-based config.
- Legacy production telephony (old dialplan): SMS in/out, delivery reports,
  voicemail — working for users today; this is the Rule-4 parity baseline.

## What must happen before this matrix can report VERIFIED

1. P0-1…P0-5 fixed and committed (S01 completion)
2. Fresh-install acceptance: wipe + redeploy from GitHub on 3p14-aaa (GSM)
   and vzu5-claw (Telegram), parity with legacy baseline
3. Real-device SMS E2E (in + out + delivery + injection test)
4. Voicemail E2E on the new dialplan
5. Voice bridge deployment + real call (S04)
6. Reboot / replug / network-loss cycles (S06.3)
