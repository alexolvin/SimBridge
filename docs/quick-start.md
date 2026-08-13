# Quick Start — SimBridge

Single-node deployment: Asterisk + agent + userbot on one machine.
**Interactive installer — two commands, ~15 min.**

---

## Prerequisites

### Hardware

- Linux server (AlmaLinux 9 or Ubuntu 22.04/24.04 tested)
- USB GSM modem (Huawei E173 tested) + SIM card with SMS capability
- Internet access (ports 443/tcp outbound — Telegram MTProto)

### Software

| Package | Notes |
|---|---|
| Python 3.9+ | installed automatically by the installer |
| Asterisk 18+ | installed by the installer (base package) |
| chan_dongle | **manual** — [wiringSoft](https://wiringSoft.com/) or `sudo add-apt-repository ppa:dongle-project/ppa` |
| Tailscale | optional — installer can install it for you |

### Telegram

- A **user** account (Bot API cannot place voice calls)
- [MTProto credentials](https://my.telegram.org/apps) → `api_id` + `api_hash`

> **Account risk:** Telegram's ToS may restrict automation.
> You are responsible for the consequences of account suspension.

---

## Step 1 — Run the Installer

```bash
curl -L https://raw.githubusercontent.com/alexolvin/SimBridge/main/deploy/install.py -o install.py
sudo python3 install.py
```

The interactive script asks:

| Prompt | What to enter |
|---|---|
| Deployment type | `Single-node (all-in-one)` |
| Node ID | hostname (default OK) |
| Modem model | e.g. `Huawei E173` (optional) |
| SIM phone number | `+79991234567` |
| chan_dongle device | `gsm` (default OK) |
| AMI password | leave empty for auto-generated |
| Telegram API_ID | from my.telegram.org |
| Telegram API_HASH | from my.telegram.org |
| Telegram username | your handle without `@` |
| Agent token | leave empty for auto-generated |
| HTTP secret | leave empty for auto-generated |
| Telegram user IDs | space-separated numeric IDs for ACL |

---

## Step 2 — Verify

The installer starts the services and runs a health check. After it finishes:

```bash
systemctl status simbridge-agent simbridge-userbot
journalctl -u simbridge-agent -u simbridge-userbot -f
```

### Telegram

Open Telegram → send `/status` → expected: system status reply.

### SMS

```
/sms +79161234567 Test message from SimBridge
```

Expected: `Отправлено` → `Доставлено`.

### Incoming call

Call your SIM number → voicemail prompt → recording forwarded to Telegram.

---

## Troubleshooting

```bash
# Agent logs
journalctl -u simbridge-agent --since '5 min ago'
# Userbot logs
journalctl -u simbridge-userbot --since '5 min ago'
# Asterisk
asterisk -rx "module show like dongle"
asterisk -rx "dongle status"
```

See `docs/troubleshooting.md` for detailed diagnostics.

---

## File Reference

| File | Edit? | Description |
|---|---|---|
| `/etc/simbridge/simbridge.yaml` | Rarely | Main config (timings, ports) |
| `/etc/simbridge/env` | Yes | Secrets (API keys, tokens) |
| `/etc/simbridge/acl.conf` | Yes | Telegram user permissions |
| `/etc/simbridge/blacklist.txt` | Yes | Blocked phone numbers |
| `/var/lib/simbridge/sim_session` | No | Telegram session (auto-created) |
