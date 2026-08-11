# Distributed Install (Two Nodes)

GSM node (Asterisk + modem) and Telegram node (userbot + bridge)
on separate machines, connected via Tailscale mesh.

## Topology

```
  TELEGRAM NODE                    GSM NODE
  ┌──────────────────┐            ┌──────────────────┐
  │ userbot (Telethon)│            │ Asterisk 18      │
  │  │ HTTP :8088     │            │   chan_dongle    │
  │                   │  TAILNET   │   simbridge-agent│
  │ tg-bridge (:5062) ├──────────► │  (localhost:8090)│
  └──────────────────┘            └──────────────────┘
         │                               │
         │ Telegram MTProto              │ USB modem
         ▼                               ▼
   Telegram Cloud                  GSM Network
```

## Prerequisites

**Both nodes:**
- AlmaLinux 8/9 or Ubuntu 22.04/24.04
- Python 3.9+
- Tailscale installed and authenticated to the same tailnet

**GSM node additionally:**
- Asterisk 18 with chan_dongle
- Huawei E173 (or compatible) on USB

**Telegram node additionally:**
- Telegram API credentials (my.telegram.org)

## Install GSM Node

```bash
# 1. Clone
git clone git@github.com:alexolvin/SimBridge.git
cd SimBridge

# 2. Run installer
sudo deploy/install.sh gsm

# 3. Configure — edit agent section
sudo vim /etc/simbridge/simbridge.yaml
# Set:
#   node.role: gsm
#   agent.listen: "100.x.x.x:8090"  (this node's Tailscale IP)
#   userbot_http.allowed_peers: ["100.y.y.y"]  (Telegram node IP)

# 4. Set secrets
sudo tee /etc/simbridge/env <<'EOF'
SIMBRIDGE_AGENT_TOKEN=your-random-hex-token
EOF
sudo chmod 0600 /etc/simbridge/env

# 5. Start
sudo systemctl restart simbridge-agent
```

## Install Telegram Node

```bash
# 1. Clone
git clone git@github.com:alexolvin/SimBridge.git
cd SimBridge

# 2. Run installer
sudo deploy/install.sh telegram

# 3. Configure
sudo vim /etc/simbridge/simbridge.yaml
# Set:
#   node.role: telegram
#   agent.listen: "100.x.x.x:8090"  (GSM node's Tailscale IP)
#   userbot_http.listen: "100.y.y.y:8088"  (this node's Tailscale IP)

# 4. Set secrets
sudo tee /etc/simbridge/env <<'EOF'
SIMBRIDGE_TG_API_ID=12345
SIMBRIDGE_TG_API_HASH=abcdef0123456789abcdef0123456789abcdef
SIMBRIDGE_AGENT_TOKEN=your-random-hex-token  (SAME as GSM node)
SIMBRIDGE_HTTP_SECRET=your-random-hex-secret
EOF
sudo chmod 0600 /etc/simbridge/env

# 5. Login to Telegram (creates session file)
sudo -u simbridge /opt/simbridge-venv/bin/python \
  -c "from telethon import TelegramClient; \
  c = TelegramClient('/var/lib/simbridge/sim_session', \
  open('/etc/simbridge/env').read())"

# 6. Start
sudo systemctl restart simbridge-userbot
```

## Verify Connectivity

From Telegram node:
```bash
# Agent is reachable
curl http://<gsm-node-tailnet-ip>:8090/v1/health \
  -H "Authorization: Bearer $SIMBRIDGE_AGENT_TOKEN"

# Userbot HTTP is reachable from GSM node
curl http://<telegram-node-tailnet-ip>:8088/health
```

## Migrating Modem to New Node

**IMPORTANT (knowledge item 11):** Do NOT move the USB modem
until the new node's configuration is fully verified.

1. Install and configure the new GSM node (without modem)
2. Verify agent starts and responds on port 8090
3. Verify Tailscale connectivity from Telegram node
4. Only then: move the USB modem
5. Verify chan_dongle registers: `asterisk -rx "dongle show status"`