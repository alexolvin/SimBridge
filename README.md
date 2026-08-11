# SimBridge

Bridge between Telegram and GSM telephony (Asterisk + chan_dongle).

SMS, voicemail, and voice calls — managed via Telegram commands on a
personal user account (MTProto, not Bot API).

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

## Stages

| Stage | Status | Description |
|---|---|---|
| 01 — Foundation | In Progress | Repo, config, secrets, agent API, ACL |
| 02 — SMS | Planned | Contacts, blacklist write, reply routing |
| 03 — Voicemail | Planned | Hardening + early-hangup gap |
| 04 — Voice Bridge | Planned | Live bidirectional voice |
| 05 — Multi-Modem | Planned | Modem pools, routing |
| 06 — Release | Planned | Hardening, monitoring, docs |
