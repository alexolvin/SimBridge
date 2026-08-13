# Distributed Install (Two Nodes)

GSM node (Asterisk + modem) and Telegram node (userbot + bridge)
on separate machines, connected via Tailscale.

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
- AlmaLinux 9 or Ubuntu 22.04/24.04
- Tailscale (installed and authenticated to the same tailnet)

**GSM node additionally:**
- USB GSM modem (Huawei E173 tested) + SIM card
- Asterisk 18 with chan_dongle (installed by installer)

**Telegram node additionally:**
- Telegram user account with API credentials ([my.telegram.org](https://my.telegram.org/apps))

## Install GSM Node

1. SSH into the GSM machine
2. Run the installer:

```bash
curl -L https://raw.githubusercontent.com/alexolvin/SimBridge/main/deploy/install.py -o install.py
sudo python3 install.py
```

3. When prompted:
   - Deployment type: `Two-node (distributed)`
   - Role: `GSM node (Asterisk + modem)`
   - Enter modem model, SIM number, Tailscale IPs
   - **Save the agent token** — you will need it on the Telegram node

## Install Telegram Node

1. SSH into the Telegram machine
2. Run the installer:

```bash
curl -L https://raw.githubusercontent.com/alexolvin/SimBridge/main/deploy/install.py -o install.py
sudo python3 install.py
```

3. When prompted:
   - Deployment type: `Two-node (distributed)`
   - Role: `Telegram node (userbot)`
   - Enter the **same agent token** from the GSM node
   - Enter GSM node's Tailscale IP
   - Enter Telegram API credentials
   - Complete Telegram login

## Verify

From the Telegram node:

```bash
# GSM agent reachable via Tailscale
curl http://<gsm-node-tailnet-ip>:8090/v1/health \
  -H "Authorization: Bearer <agent-token>"
```

From Telegram:
- Send `/status` — should show system status
- Send `/sms +79991234567 Hello` — should deliver via GSM node

## Migrating Modem to New Node

**Do NOT move the USB modem until the new node is fully verified.**

1. Install and configure the new GSM node (without modem)
2. Verify agent starts and responds on port 8090
3. Verify Tailscale connectivity from Telegram node
4. Only then: move the USB modem
5. Verify: `asterisk -rx "dongle show status"`

## Re-running

The installer detects existing installations:

```bash
sudo python3 install.py
```

Choose `Update in place` or `Remove existing and start fresh`.
