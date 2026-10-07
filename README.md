# SimBridge

**Languages:** English | [Русский](README.ru.md)

Bridge between Telegram and GSM telephony (Asterisk + chan_dongle).

SMS, voicemail, and live voice calls — managed via Telegram commands on a
personal user account (MTProto, not Bot API). Built to financial-grade standards:
failures can cost real money, so correctness takes priority over speed.

**Requirements:** Asterisk 18+ on EL9/Ubuntu, `chan_dongle` + USB/Mini PCIe GSM modem (Quectel EC25-EUX tested),
a Telegram user account (Bot API cannot place voice calls), Tailscale for distributed deployments.

**⚠️  Account risk:** This uses a Telegram user account. Telegram's Terms of Service may
restrict automation. You are responsible for the consequences of account suspension.

## Architecture

```
  Telegram ◄────────────────────────────────────────►
               (MTProto + WebRTC)

  ┌─────────────────── TELEGRAM NODE ────────────────────┐
  │                                                      │
  │  userbot (Telethon)         tg-bridge (ntgcalls<->SIP│
  │       │                          │                   │
  │       │ HTTP (control)           │ SIP 5062 + RTP    │
  └──────────────────────────────────────────────────────┘
          │        TAILSCALE        │
          │                         │
  ┌────────────────────── GSM NODE ──────────────────────┐
  │                                                      │
  │  simbridge-agent          Asterisk 18                │
  │                             │                        │
  │                        chan_dongle                   │
  └──────────────────────────────────────────────────────┘
                                    │
                                    GSM modem / SIM
```

- **userbot** — Telegram user account (Telethon). Receives commands, forwards SMS/voicemail to Telegram.
- **simbridge-agent** — HTTP+JSON API on the GSM node. Replaces the old SSH+shell-interpolation path.
- **tg-bridge** — Voice media bridge (Telegram WebRTC ↔ SIP). Stage 04.
- **core** — Shared: config, ACL, audit, rate limiting, secret detection.

## Quick Start

Download and run the single-file interactive installer:

```bash
curl -L https://raw.githubusercontent.com/alexolvin/SimBridge/main/deploy/install.py -o install.py
sudo python3 install.py
```

The installer will:
- Detect OS, Python, Asterisk, chan\_dongle, Tailscale, USB modems
- Prompt for missing configuration (Telegram credentials, modem type, secrets)
- Clone the SimBridge repository, install dependencies, create systemd units
- Configure the selected deployment (single-node or distributed)
- Guide you through Telegram login and first SMS/call tests

Or from a cloned repository:

```bash
git clone https://github.com/alexolvin/SimBridge.git && cd SimBridge
sudo python3 deploy/install.py
```

See `docs/quick-start.md` or `docs/install-distributed.md` for full guides.

## Commands

| Command | Permission | Description |
|---|---|---|
| `<number>` | `out_call` | Voice call (simply enter the number) |
| `/redial` | `out_call` | Redial the last number |
| `/add <name> [number]` | `out_call` | Add a contact to the phonebook |
| `/show` | `out_call` | Show the phonebook |
| `/del <name\|number>` | `out_call` | Delete from the phonebook |
| `/call <name\|number>` | `out_call` | Call a contact from the phonebook |
| `/sms <name\|number> <message>` | `out_sms` | Send an SMS |
| `/block <number>` | `out_sms` or `out_call` | Block a number |
| `/listblock` | `out_sms` or `out_call` | Show blocked numbers |
| `/unblock <number>` | `out_sms` or `out_call` | Unblock a number |
| `/help` | — | Show available commands (filtered by permissions) |

Incoming SMS and voicemail are forwarded automatically to users with `in_sms` / `in_call` rights.

## Hardware

- **Modem:** Quectel EC25-EUX (Cat 6, Mini PCIe) — tested with A09 firmware.
- **⚠️ Flash ban:** Do NOT flash, FOTA-update, or power-cycle (`AT+QPOWD`) the modem. A13+ firmware carries a geo-registration lock (RUS) and irreversible anti-rollback. Bricking the module = permanent loss.

## Removal

### Single-node (all-in-one)

```bash
# 1. Stop services
sudo systemctl stop simbridge-userbot simbridge-agent

# 2. Disable services (no auto-start on reboot)
sudo systemctl disable simbridge-userbot simbridge-agent

# 3. Remove systemd units
sudo rm -f /etc/systemd/system/simbridge-userbot.service
sudo rm -f /etc/systemd/system/simbridge-agent.service
sudo systemctl daemon-reload

# 4. Remove configuration
sudo rm -rf /etc/simbridge/

# 5. Remove data (recordings, sessions, cache)
sudo rm -rf /var/lib/simbridge/

# 6. Remove logs
sudo rm -rf /var/log/simbridge/

# 7. Clean up Asterisk chan_dongle (if installed separately)
#    Uncomment in /etc/asterisk/modules.conf:
#    noload => chan_dongle.so
#    Then: sudo systemctl restart asterisk

# 8. Remove Tailscale (if installed for SimBridge only)
sudo tailscale down
sudo systemctl stop tailscaled
sudo systemctl disable tailscaled
sudo apt remove tailscale   # Ubuntu
# OR
sudo yum remove tailscale   # EL9

# 9. Remove project directory
cd ~/projects
rm -rf SimBridge
```

### Distributed (two-node)

**GSM Node (Asterisk + agent):**
```bash
# 1. Stop service
sudo systemctl stop simbridge-agent
sudo systemctl disable simbridge-agent

# 2. Remove systemd unit
sudo rm -f /etc/systemd/system/simbridge-agent.service
sudo systemctl daemon-reload

# 3. Remove configuration
sudo rm -rf /etc/simbridge/

# 4. Remove data
sudo rm -rf /var/lib/simbridge/

# 5. Remove logs
sudo rm -rf /var/log/simbridge/

# 6. Clean up Asterisk chan_dongle (see single-node above)
```

**Telegram Node (userbot + tg-bridge):**
```bash
# 1. Stop services
sudo systemctl stop simbridge-userbot
# If using Docker for tg-bridge:
sudo docker compose -f ~/projects/SimBridge/deploy/docker-compose.yml down --remove-orphans

# 2. Disable services
sudo systemctl disable simbridge-userbot

# 3. Remove systemd unit
sudo rm -f /etc/systemd/system/simbridge-userbot.service
sudo systemctl daemon-reload

# 4. Remove configuration
sudo rm -rf /etc/simbridge/

# 5. Remove data
sudo rm -rf /var/lib/simbridge/

# 6. Remove logs
sudo rm -rf /var/log/simbridge/

# 7. Remove project directory
cd ~/projects
rm -rf SimBridge
```

> **⚠️ Warning:** Removal is irreversible. Ensure no users are actively using the system before proceeding. Back up `/etc/simbridge/env` if you plan to re-deploy later (it contains API keys and tokens).

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
