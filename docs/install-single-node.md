# Single-Node Install

All services (Asterisk, agent, userbot) on one machine.
Suitable for: development, small deployments, single-location setups.

## Prerequisites

- AlmaLinux 8/9 or Ubuntu 22.04/24.04
- Python 3.9+
- Asterisk 18 with chan_dongle
- Huawei E173 (or compatible GSM modem) on USB
- Tailscale installed and authenticated
- Telegram account with API credentials (my.telegram.org)

## Architecture

```
┌──────────────────────────────────────────────┐
│                SINGLE NODE                   │
│                                              │
│  Telegram ◄──► userbot (Telethon)            │
│             ▲                                │
│             │ HTTP (localhost:8088)           │
│  Asterisk 18 (5060)                          │
│    ├── chan_dongle (GSM modem)               │
│    └── hooks ──► localhost:8088/events       │
│                                              │
│  simbridge-agent (localhost:8090)             │
│    └── AMI ──► Asterisk (5038)              │
│                                              │
│  (no tg-bridge on single-node until stage 04)│
└──────────────────────────────────────────────┘
```

## Quick Install

```bash
# 1. Clone
git clone git@github.com:alexolvin/SimBridge.git
cd SimBridge

# 2. Run installer
sudo deploy/install.sh all-in-one

# 3. Configure
sudo vim /etc/simbridge/simbridge.yaml  # set your values

# 4. Set secrets
sudo tee /etc/simbridge/env <<'EOF'
SIMBRIDGE_TG_API_ID=12345
SIMBRIDGE_TG_API_HASH=abcdef0123456789abcdef0123456789abcdef
SIMBRIDGE_AGENT_TOKEN=your-random-hex-token
SIMBRIDGE_HTTP_SECRET=your-random-hex-secret
EOF
sudo chmod 0600 /etc/simbridge/env

# 5. Add ACL users
sudo vim /etc/simbridge/acl.conf
# Add: 1234567 out_sms in_sms

# 6. Start
sudo systemctl restart simbridge-agent
sudo systemctl restart simbridge-userbot

# 7. Verify
systemctl status simbridge-agent
systemctl status simbridge-userbot
journalctl -u simbridge-agent -f
```

## Asterisk AMI Setup

Create `/etc/asterisk/manager.conf` (if not present):
```ini
[general]
enabled = yes
port = 5038
bindaddr = 127.0.0.1

[simbridge]
secret = your-ami-password
read = system,call,log,verbose,command,agent,user
write = system,call,log,verbose,command,agent,user
```

Add the AMI password to the agent's environment:
```bash
echo 'SIMBRIDGE_AMI_PASSWORD=your-ami-password' | sudo tee -a /etc/simbridge/env
```

## Verify

```bash
# Agent health
curl http://127.0.0.1:8090/v1/health

# Modem status
curl -H "Authorization: Bearer $SIMBRIDGE_AGENT_TOKEN" \
     http://127.0.0.1:8090/v1/modems

# Agent logs
journalctl -u simbridge-agent --since "5 min ago"

# Userbot logs
journalctl -u simbridge-userbot --since "5 min ago"
```

## chan_dongle Note

chan_dongle is NOT in standard AlmaLinux/Ubuntu repos.
- AlmaLinux: build from source or use a prebuilt RPM
  (see https://wiringSoft.com/)
- Ubuntu: `sudo add-apt-repository ppa:dongle-project/ppa`

After installing, verify with `asterisk -rx "module show like dongle"`.