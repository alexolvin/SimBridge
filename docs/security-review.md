# S06.1 — Security Review Against Threat Model

Date: 2026-08-12
Scope: Full repository — `main` branch at HEAD.

---

## Threat Model — Eight Threats (GPT §60)

### T1 — Unauthorized Telegram User

**Status: MITIGATED**

- **Default-deny ACL** (`core/acl.py`): Unknown user IDs are denied all four rights (`in_sms`, `in_call`, `out_sms`, `out_call`). The `check()` method returns `False` for any UID not in `acl.conf`.
- **Every Telegram command checks ACL** before performing any action (`userbot/userbot.py`): `/sms`, `/broadcast`, `/block`, `/unblock` all verify `out_sms`; voice note handler checks `in_call`.
- **Denied attempts are audited** (`EventType.USER_DENIED`) with user ID, right, and command.
- **Verified**: A non-ACL account would receive "Access denied" for every command and be silently ignored for passive rights (`in_sms`, `in_call`).

### T2 — Stolen Telegram Session

**Status: MITIGATED**

- **Session file permissions** (`install.sh:179`): `chmod 0600` on `sim_session.session`. Only the `simbridge` system user can read/write.
- **Never in git** (`.gitignore`): `*.session` and `*.session-journal` are excluded.
- **Session path** (`config/simbridge.example.yaml`): `/var/lib/simbridge/sim_session` — outside project directory.
- **Recommendation**: Encrypt backups containing session files. This is documented in install guides.

**Known limitation**: If an attacker gains root access to the Telegram node, the session can be extracted. This is a general system-compromise scenario; there is no additional protection at the application level beyond file permissions.

### T3 — Stolen API Token

**Status: MITIGATED (with fix applied in S06.1)**

- **Dual-factor API auth** (`agent/deps.py`): Bearer token + IP allowlist. Both must be valid for access.
- **Timing-safe comparison** (FIX APPLIED): Token comparison now uses `hmac.compare_digest()` instead of `!=`, preventing remote token-length inference. Previously vulnerable to a timing side-channel.
- **Per-node tokens**: Each node has its own token. Tokens are never shared.
- **Rotation procedure**: Update `SIMBRIDGE_AGENT_TOKEN` env var in systemd override, restart agent, update peer's config.
- **Token stored in env only** (`agent.token_env`): Never in YAML config.

### T4 — Compromised GSM Node

**Status: PARTIALLY MITIGATED — ACCEPTED RISK**

- **Per-node identity** (S05.1): `node.id` in config provides logical identity, but not cryptographic identity. A compromised GSM node could impersonate itself to the Telegram node using the shared agent token.
- **Damage scope**: Compromised GSM node can:
  - Send arbitrary SMS via `DongleSendSMS` (physical modem access)
  - Inject false delivery reports
  - Hang up active calls
  - Access audit log (read)
- **Not reachable from GSM node**: Telegram session, userbot HTTP server (one-way: GSM → Telegram events only).
- **Mitigation for multi-node**: S05.3 (not implemented — no second node planned) would add per-node cryptographic identity.

**Accepted risk**: In a single-node or two-node deployment over Tailscale, a compromised GSM node has physical access to the modem anyway. The threat model assumes the physical host is trusted.

### T5 — Replay

**Status: MITIGATED**

- **Correlation-ID replay window** (`agent/deps.py`): Duplicate `x-correlation-id` headers within the configured window (default: 300s) are rejected with HTTP 409.
- **Configurable**: `limits.replay_window_seconds` in config (S06.1 fix: now properly read from config via `init_deps`).
- **Cleanup**: Expired entries are pruned on each check.

**Known limitation**: The replay store is in-memory. A process restart resets the window. This is acceptable for telephony-scale traffic; a persistent store would add complexity without proportional benefit.

### T6 — Flood

**Status: MITIGATED (with fix applied in S06.1)**

- **SMS rate limiter** (`core/ratelimit.py`): Per-user sliding-window. Configured via `limits.sms_per_hour` (default: 30/hour). Enforced on `/v1/sms`.
- **Call rate limiter** (FIX APPLIED): Per-user sliding-window. Configured via `limits.calls_per_minute` (default: 3/min). Now enforced on `/v1/call/outgoing`. Previously missing.
- **Verified**: Rate-limited requests return HTTP 429 with a user-friendly message.

**Known limitation**: Rate limits are per-user, not global. A coordinated attack from multiple users could exceed modem capacity. At telephony scale (few users), this is acceptable.

### T7 — Stuck Modem

**Status: NOT MITIGATED — DEFERRED TO S06.2**

- **Current state**: The `SingleModemProvider` (`core/modem.py`) derives state from real device reports (`dongle show devices`), but there is no automated watchdog or recovery.
- **Manual recovery**: Admin can restart the agent service or reset the modem via AMI.
- **Plan**: S06.2 will implement watchdog with automatic reset attempt, alerting after repeated failures.

### T8 — Network Partition

**Status: MITIGATED**

- **Clean call teardown** (S04.4): `terminate_bridged_calls()` in `core/call_control.py`. If the tailnet drops mid-call, both legs are terminated and the user is notified.
- **Link drop detection** (S04.4): PJSIP transport monitoring detects tailnet loss.
- **No orphan channels**: Symmetric hangup (`routes.py:call_hangup`) terminates both GSM and bridge legs.
- **Verified by**: Link drop test (S04.4) — `tailscale down` on one side during active call.

---

## Additional Security Checks

### No 0.0.0.0 Binds (FIX APPLIED)

**Before**: The config schema had no validation for bind addresses. A misconfigured `agent.listen: "0.0.0.0:8090"` would expose the API to all interfaces.

**Fix**: `load_config()` now validates `agent.listen` and `userbot_http.listen` and raises `ConfigError` if the host is `0.0.0.0`.

**Verification**: Test added (`tests/test_foundation.py`) — config with `0.0.0.0` is rejected at startup.

### SSH-Based Control Removed

**Result**: Repo-wide `grep -rnE 'ssh|scp|rsync'` across `agent/`, `userbot/`, `core/`, `scripts/`, `deploy/`, `asterisk/` returns **no matches**. All inter-node control goes through authenticated HTTP API (JSON, not shell-interpolated).

### Secrets Scan

**Result**: Working tree scan with `core/secrets_check.py` returns 151 matches, but **all are false positives**:
- E.164 phone numbers in test fixtures, comments, and documentation
- Deliberate secret patterns in `tests/test_foundation.py` (testing the detector)
- Session file references in `deploy/install.sh` (chmod commands, not commit)
- `core/secrets_check.py` itself (pattern definitions)

**Pre-commit hook** (`scripts/pre-commit.sh`): Active. Blocks commits containing secret patterns.

**Known gap**: The hook checks only the working tree (staged files). It does not scan git history. A force-push or direct git command could bypass it. Mitigation: CI check on push (deferred to S06.4).

### IP Allowlist Fix (FIX APPLIED)

**Before**: `check_ip()` in `deps.py` returned early (allow all) when `_allowed_peers` was empty — effectively a dev-mode bypass.

**Fix**: `check_ip()` now:
1. Always allows localhost (`127.0.0.1`, `::1`) for single-node mode.
2. Rejects non-localhost connections if `_allowed_peers` is empty.
3. Checks allowlist as before when configured.

### Timing-Safe Comparisons (FIX APPLIED)

**Before**: `deps.py:check_auth` used `!=` for token comparison. `http_server.py` used `!=` for secret comparison. Both vulnerable to timing side-channel attacks.

**Fix**: Both now use `hmac.compare_digest()` for constant-time string comparison.

---

## Firewall Recommendations

| Port | Service | Should be open on | Blocked from |
|---|---|---|---|
| 5060 | Asterisk PJSIP | GSM node, localhost | Internet |
| 5062 | tg-bridge SIP | GSM node (distributed), localhost | Internet |
| 8090 | Agent API | Telegram node Tailscale IP only | All other IPs |
| 8088 | Userbot HTTP | GSM node Tailscale IP only | All other IPs |
| 10000-20000/udp | RTP media | GSM node ↔ Telegram node | Internet |
| 5038 | AMI | localhost only | All non-localhost |

**Tool**: `firewalld` (EL9) or `ufw` (Ubuntu). Example rules in `docs/install-*.md`.

---

## Sudoers

The `install.sh` script creates a system user (`simbridge`) with `--no-create-home --shell /usr/sbin/nologin`. The agent runs as this unprivileged user. AMI access to Asterisk is via network socket (port 5038), not sudo.

**Current sudo requirement**: None identified in the codebase. The old SSH-based path (which required `sudo /usr/local/bin/send-sms-report.sh`) has been fully removed.

---

## Summary of S06.1 Fixes

| Fix | Severity | File | Description |
|---|---|---|---|
| Timing-safe token comparison | HIGH | `agent/deps.py` | `hmac.compare_digest()` for bearer token |
| Timing-safe secret comparison | HIGH | `userbot/http_server.py` | `hmac.compare_digest()` for HTTP secret |
| IP allowlist enforcement | HIGH | `agent/deps.py` | Reject non-localhost when no peers configured |
| Call rate limiting | MEDIUM | `agent/routes.py`, `agent/agent.py`, `agent/deps.py` | Per-user rate limit on `/call/outgoing` |
| Bind-address validation | MEDIUM | `core/config.py` | Reject `0.0.0.0` in listen addresses |
