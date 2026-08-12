# SimBridge

Bridge between Telegram and GSM telephony (Asterisk + chan_dongle).

SMS, voicemail, and live voice calls — managed via Telegram commands on a
personal user account (MTProto, not Bot API). Built to financial-grade standards:
failures can cost real money, so correctness takes priority over speed.

**Requirements:** Asterisk 18+ on EL9/Ubuntu, `chan_dongle` + USB GSM modem (Huawei E173 tested),
a Telegram user account (Bot API cannot place voice calls), Tailscale for distributed deployments.

**⚠️  Account risk:** This uses a Telegram user account. Telegram's Terms of Service may
restrict automation. You are responsible for the consequences of account suspension.

## Architecture

```
                    ┌─────────────────── TELEGRAM NODE ──────────────────┐
   Telegram ◄──────►│  userbot (Telethon)     tg-bridge (ntgcalls↔SIP)   │
   (MTProto+WebRTC) │       │ control                    │ SIP 5062      │
                    └───────┼────────────────────────────┼───────────────┘
                            │  authenticated HTTP        │  SIP + RTP
                            │  (control plane)           │  (media plane)
                       ─────┼────────── TAILSCALE ───────┼─────
                            │                            │
                    ┌───────┼────────────────────────────┼─── GSM NODE ──┐
                    │  simbridge-agent            Asterisk 18 (5060)     │
                    │                                    │ chan_dongle   │
                    └────────────────────────────────────┼───────────────┘
                                                    GSM modem / SIM
```

- **userbot** — Telegram user account (Telethon). Receives commands, forwards SMS/voicemail to Telegram.
- **simbridge-agent** — HTTP+JSON API on the GSM node. Replaces the old SSH+shell-interpolation path.
- **tg-bridge** — Voice media bridge (Telegram WebRTC ↔ SIP). Stage 04.
- **core** — Shared: config, ACL, audit, rate limiting, secret detection.

## Quick Start

```bash
# Clone
git clone git@github.com:alexolvin/SimBridge.git
cd SimBridge

# Single-node install (Asterisk + agent + userbot on one machine)
sudo deploy/install.sh all-in-one

# Configure
sudo cp config/simbridge.example.yaml /etc/simbridge/simbridge.yaml
sudo vim /etc/simbridge/simbridge.yaml

# Set secrets (NEVER commit these)
sudo tee /etc/simbridge/env <<'EOF'
SIMBRIDGE_TG_API_ID=12345
SIMBRIDGE_TG_API_HASH=...
SIMBRIDGE_AGENT_TOKEN=...
SIMBRIDGE_HTTP_SECRET=...
EOF
sudo chmod 0600 /etc/simbridge/env

# Start
sudo systemctl restart simbridge-agent simbridge-userbot
```

See `docs/install-single-node.md` or `docs/install-distributed.md` for full guides.

## Commands

| Command | Permission | Description |
|---|---|---|
| `/sms <phone> <message>` | `out_sms` | Send SMS |
| `/broadcast <message>` | `out_sms` | Send to all users |
| `/help` | — | Show available commands |

Incoming SMS and voicemail are forwarded automatically to users with `in_sms` / `in_call` rights.

## Project Structure

```
simbridge/
├── agent/              # GSM node: HTTP API + Asterisk AMI client
├── userbot/            # Telegram node: Telethon client
├── bridge/             # Telegram node: voice media bridge (stage 04)
├── core/               # Shared: config, ACL, audit, rate limiting
├── config/
│   ├── simbridge.example.yaml
│   └── blacklist.example.txt
├── deploy/
│   ├── systemd/
│   ├── docker-compose.yml
│   └── install.sh
├── docs/
│   ├── install-single-node.md
│   ├── install-distributed.md
│   ├── voice-bridge.md
│   └── troubleshooting.md
├── scripts/            # Asterisk hook scripts
└── tests/
```

## Security

- Secrets never enter git (pre-commit hook + CI check)
- All secrets via environment variables, referenced by name in config
- Agent API: bearer token + IP allowlist (both required)
- Replay protection: duplicate correlation_ids rejected within a time window
- SMS text passed as structured AMI fields — never shell-interpolated
- Timing-safe secret comparisons (`hmac.compare_digest`)
- Bind-address validation: refuses `0.0.0.0` at startup

## Observability

- Structured JSON logs with UTC timestamps and correlation IDs
- Health endpoint: `/v1/health` — component status (asterisk, modem, peer, bridge) + metrics
- Metrics: SMS in/out, delivery rate, call outcomes, modem registration state
- Alerting: Telegram notifications for critical events (dongle offline, session invalid, etc.)
- Automatic recovery: AMI reconnect with exponential backoff, modem watchdog

## Limitations

- **Voice bridge requires `foobar26/tg2sip`** — a third-party Docker service. Must be deployed separately.
- **Single SIM per GSM node** — multi-modem pools are implemented but tested with one member.
- **No Bot API for voice calls** — uses a Telegram user account (see account risk above).
- **Real-device evidence required** — SMS/voice acceptance criteria need physical modem access.
- **No gRPC** — HTTP+JSON is used for all inter-node communication.

## Stages

| Stage | Status | Description |
|---|---|---|
| 01 — Foundation | ✅ Complete | Repo, config, secrets, agent API, ACL |
| 02 — SMS | ✅ Complete | Contacts, blacklist, correlation, reply routing |
| 03 — Voicemail | ✅ Complete | Hardening, early-hangup, temp cleanup |
| 04 — Voice Bridge | ✅ Complete | tg2sip fork, call state machines, distributed |
| 05 — Multi-Modem | ⚠️ Partial | Modem abstraction + pools (S05.1+S05.2). S05.3 deferred — no second node. |
| 06 — Release | 🔄 In Progress | Security review, observability, resilience, docs |

## License

SimBridge is licensed under the [MIT License](LICENSE). See `LICENSE` for details.
