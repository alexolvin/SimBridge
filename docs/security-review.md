# S06.1 — Security Review Against Threat Model

Date: 2026-08-12; revised 2026-08-16 (S06.1 audit pass + S06.2 wiring)
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

- **Session file permissions** (`deploy/install.py`): `chmod 0600` on `sim_session.session`. Only the `simbridge` system user can read/write.
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
- **Mitigation for multi-node**: per-node cryptographic identity is out of scope for the current two-node rollout; the Tailscale network + per-role tokens bound the exposure to what is listed above.

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

**Status: MITIGATED (S06.2, code-level)**

- **State derives from the real device**: the poller (`agent/modem_poll.py`) queries `DongleShowDevices` via AMI every `watchdog.modem_check_seconds` and feeds `SingleModemProvider`; an absent device drives the provider OFFLINE.
- **Automated watchdog** (`core/recovery.py` `ModemWatchdog`, wired in `agent/agent.py`): while the provider is unavailable it attempts a reset — the AMI reconnect path (`ami.connect()` re-establishes the Asterisk management session) — up to 3 times, then alerts the master (`dongle_offline`, 300 s cooldown per the default alert rule; recovery alerts use a 600 s cooldown).
- **Edge-triggered alerts** (`agent/supervisor.py`): present→absent (`dongle_offline`), registration lost (`gsm_registration_lost`), recovered (`modem_recovery`), peer unreachable/recovery — the master user is paged on transitions, not every 30 s cycle.
- **Known limitation**: no verified USB-level reset action exists in the chan_dongle AMI vocabulary (only `DongleShowDevices` / `DongleSendSMS` / `DongleDeviceEntry` are known to this repo — Rule 2), so the reset attempt is the AMI session reconnect, not a hardware reset. Live unplug/replug behavior = Pass C (MANUAL_VERIFY).

### T8 — Network Partition

**Status: MITIGATED**

- **Clean call teardown** (S04.4): `terminate_bridged_calls()` in `core/call_control.py`. If the tailnet drops mid-call, both legs are terminated and the user is notified.
- **Link drop detection** (S04.4): PJSIP `rtptimeout=60` / `rtpholdtimeout=30` drop dead RTP legs; the check-timeouts dialplan extension force-terminates stuck BRIDGED calls (audit `partial_hangup`).
- **No orphan channels**: Symmetric hangup (`routes.py:call_hangup`) terminates both GSM and bridge legs.
- **Status**: CODE+UNITS (teardown paths unit-tested). Live `tailscale down` mid-call evidence = Pass C (MANUAL_VERIFY — see HANDOFF-S04).

---

## Additional Security Checks

### No 0.0.0.0 Binds (FIX APPLIED)

**Before**: The config schema had no validation for bind addresses. A misconfigured `agent.listen: "0.0.0.0:8090"` would expose the API to all interfaces.

**Fix**: `load_config()` now validates `agent.listen` and `userbot_http.listen` and raises `ConfigError` if the host is `0.0.0.0`.

**Verification**: Test added (`tests/test_foundation.py`) — config with `0.0.0.0` is rejected at startup.

### PJSIP Wildcard Bind (FOUND & FIXED in S06.1 audit)

**Finding**: In distributed mode (`voice.bridge_host != 127.0.0.1`), `scripts/generate_asterisk_config.py::generate_pjsip` emitted `bind=0.0.0.0` whenever `SIMBRIDGE_NODE_TAILSCALE_IP` was unset — a wildcard SIP listener on every interface, protected only by the (mandatory) inbound auth. A wildcard bind is a finding, not a feature: it exposes the SIP port to any interface the host has.

**Fix**: the generator now binds the PJSIP transport to the Tailscale interface IP in distributed mode, and **refuses to generate** `pjsip.conf` (exit 1) when `SIMBRIDGE_NODE_TAILSCALE_IP` is not set. Single-node mode binds `127.0.0.1` only.

**Verification**: `tests/test_s06_wiring.py::TestPjsipBind` — distributed+IP → `bind=<tailscale-ip>` and no `0.0.0.0` in the output; distributed without IP → `SystemExit(1)`; single node → `bind=127.0.0.1`.

### `/health` Endpoints Are Unauthenticated (documented scope decision)

Both nodes' `/health` endpoints accept unauthenticated requests: the response is operational state (component health, counters, session state) — no secrets, no control surface. They bind to the tailnet/loopback only (see bind rules above) and are consumed by the peer's health checker. Documented here so the choice is a decision, not an accident.

### SSH-Based Control Removed

**Result** (re-checked 2026-08-16): no SSH/SCP/rsync is used for **inter-node control**. The only `ssh` references in the repo are the installer's git-clone fallback (`deploy/install.py`, repo download only) and docstrings describing the replaced legacy path. All inter-node control goes through the authenticated HTTP API (JSON, not shell-interpolated).

### Secrets Scan

**Working tree** (re-run 2026-08-16, S06.4): `python3 core/secrets_check.py $(git ls-files)` → **exit 0**. The non-blocking warnings are detector self-test content (deliberate secret patterns in `tests/test_foundation.py` / `tests/test_secrets_check.py`) and session-file path references in `deploy/install.py` (chmod/chown logic, not secrets).

**Git history** (re-run 2026-08-16, S06.4): `python3 scripts/scan_history_secrets.py` → **`HISTORY CLEAN: no blocking secret hits across 297 blobs (435 unique blob paths)`, exit 0** (non-blocking `session_file_ref` warnings only). The earlier "history scan deferred" gap is closed: the full history is scanned, not just the working tree.

**Pre-commit hook** (`scripts/pre-commit.sh`): Active. Blocks commits containing secret patterns.

**CI**: the `secrets` job in `.github/workflows/ci.yml` runs both the working-tree scan and the git-history scan on every push.

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

The `deploy/install.py` installer creates a system user (`simbridge`) with `--no-create-home --shell /usr/sbin/nologin`. The agent runs as this unprivileged user. AMI access to Asterisk is via network socket (port 5038), not sudo.

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
| PJSIP wildcard bind (S06.1 audit) | MEDIUM | `scripts/generate_asterisk_config.py` | Distributed mode binds the Tailscale IP; generation refused without `SIMBRIDGE_NODE_TAILSCALE_IP` (no `0.0.0.0` SIP listener) |

## S06.2 Observability & Recovery (wired 2026-08-16)

Not a security control per se, but closes the T7 recovery gap and the
"silently broken system" failure mode:

- Structured JSON logging with correlation IDs on both nodes (`core/logging_config.py`); every agent request and userbot event echoes or mints `x-correlation-id` (middleware in `agent/agent.py` and `userbot/http_server.py`).
- Metrics (`core/metrics.py`): SMS sent/delivered/failed/incoming + delivery rate; call counts by outcome per direction + answered durations (`avg_answered_seconds`); component state (modem registered, bridge reachable, telegram connected). Exposed at both `/health` endpoints.
- Real health endpoints: the userbot's `/health` (previously a stub) reports `telegram_connected` + metrics; the agent's reports all components. The agent's supervisor (`agent/supervisor.py`) reads the peer's `/health` and feeds edge-triggered alerts.
- Alerts to the master: the agent has no Telegram session, so its `AlertManager` posts to the userbot's new `/events/alert` endpoint (secret + IP allowlist, audited `ALERT_SENT`); the userbot alerts the master directly.
- Auto-recovery: modem watchdog (reset attempt = verified AMI reconnect, alert after 3 failed resets), AMI session auto-reconnect on ConnectionError (`agent/ami_reconnect.py`), Telegram session backoff reconnect (`userbot/userbot.py::run_with_recovery`, re-auth procedure in `docs/re-auth.md`).
- Wiring covered by `tests/test_s06_wiring.py` (41 tests).
