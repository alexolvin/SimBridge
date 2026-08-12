# Quick Start — SimBridge

Single-node deployment: Asterisk + agent + userbot on one machine.

**Target time:** 30 min (if Asterisk + chan_dongle are already installed).

---

## Prerequisites

### Hardware

- Linux server (AlmaLinux 9 or Ubuntu 22.04/24.04 tested)
- USB GSM modem (Huawei E173 tested) + SIM card with SMS capability
- Internet access (for Telegram MTProto — ports 443/tcp outbound)

### Software

| Package | Version | Source |
|---|---|---|
| Python | 3.9+ | system repo |
| Asterisk | 18+ | EL9 AppStream / Ubuntu PPA |
| chan_dongle | latest | [wiringSoft](https://wiringSoft.com/) or PPA |
| ffmpeg | any | system repo (voicemail normalization) |
| curl | any | system repo (hook scripts) |

### Telegram Account

- A **user** account (Bot API cannot place voice calls)
- [MTProto API credentials](https://my.telegram.org/apps) → `api_id` + `api_hash`

> **Account risk:** Telegram's Terms of Service may restrict automation.
> You are responsible for the consequences of account suspension.

---

## Step 1 — Clone and Install

```bash
git clone git@github.com:alexolvin/SimBridge.git
cd SimBridge

# Installs: Python venv, systemd units, directories, default configs
sudo deploy/install.sh all-in-one
```

The installer creates:

| Path | Purpose |
|---|---|
| `/etc/simbridge/simbridge.yaml` | Main config (from `config/simbridge.example.yaml`) |
| `/etc/simbridge/acl.conf` | User permissions (empty by default) |
| `/etc/simbridge/blacklist.txt` | Blocked phone numbers |
| `/etc/simbridge/env` | Secrets (created by you, see Step 3) |
| `/opt/simbridge/` | Application code |
| `/opt/simbridge-venv/` | Python virtualenv |
| `/var/lib/simbridge/` | Runtime data (session, recordings) |

---

## Step 2 — Configure Asterisk

### 2a. AMI Access

Create `/etc/asterisk/manager_custom.conf`:

```ini
[simbridge]
secret = YOUR-AMI-PASSWORD
read = system,call,log,verbose,command,agent,user
write = system,call,log,verbose,command,agent,user
```

Make sure `/etc/asterisk/manager.conf` includes it:

```ini
[general]
enabled = yes
port = 5038
bindaddr = 127.0.0.1
```

### 2b. Verify chan_dongle

```bash
asterisk -rx "module show like dongle"
# Expected: chan_dongle.so (loaded)
```

If not loaded, install from [wiringSoft](https://wiringSoft.com/) or:

```bash
# Ubuntu
sudo add-apt-repository ppa:dongle-project/ppa
sudo apt install chan-dongle
```

Check that the modem is recognized:

```bash
asterisk -rx "dongle status"
# Expected: Dongle 'gsm' — registered, signal strength XX%
```

### 2c. Dialplan

Create `/etc/asterisk/extensions_custom.conf` (include from `extensions.conf` via `[general]` → `staticincludes = yes`):

```ini
[simbridge-inbound]
exten => s,1,Answer()
same => n,Set(CORRELATION=${UNIQUEID})
same => n,Set(CALL_FROM=${CHANNEL(CIDNUMBER)})
same => n,Wait(1)
same => n,Playback(custom/vm-prompt)
same => n,Set(VMFILE=/var/lib/simbridge/recordings/${CORRELATION}.wav)
same => n,MixMonitor(${VMFILE}|,HANGUP_HANDLER())
same => n,WaitExten(24)

[simbridge-hangup]
exten => h,1,ExecIf($["${EXTR_INTERFACE}" = ""] & ${VMFILE} != ""?System(/opt/simbridge/scripts/tg-voice-forward.sh ${CORRELATION} ${CALL_FROM} ${VMFILE}))

[simbridge-sms]
exten => s,1,System(/opt/simbridge/scripts/tg-sms-forward.sh)
```

Copy hook scripts:

```bash
sudo cp scripts/tg-sms-forward.sh /opt/simbridge/scripts/
sudo cp scripts/tg-voice-forward.sh /opt/simbridge/scripts/
sudo chmod +x /opt/simbridge/scripts/tg-*.sh
```

### 2d. Generate Asterisk Globals

This reads `simbridge.yaml` and writes timings to Asterisk config (one source of truth):

```bash
sudo /opt/simbridge-venv/bin/python3 scripts/generate_asterisk_config.py /etc/simbridge/simbridge.yaml
```

Output: `/etc/asterisk/asterisk-globals.conf` (auto-generated, do not edit manually).

### 2e. Voicemail Prompt

Record or download a prompt in G.711 u-law format:

```bash
sudo mkdir -p /var/lib/asterisk/sounds/custom
# Convert your recording to u-law:
ffmpeg -i your-prompt.wav -ac 1 -ar 8000 -c:a pcm_mulaw /var/lib/asterisk/sounds/custom/vm-prompt.ulaw
```

### 2f. Create Recordings Directory

```bash
sudo mkdir -p /var/lib/simbridge/recordings
sudo chown simbridge:nogroup /var/lib/simbridge/recordings
```

### 2g. Reload Asterisk

```bash
sudo systemctl restart asterisk
asterisk -rx "core show registry"   # confirm chan_dongle is up
```

---

## Step 3 — Set Secrets

Create `/etc/simbridge/env`:

```bash
sudo tee /etc/simbridge/env <<'EOF'
SIMBRIDGE_TG_API_ID=12345678
SIMBRIDGE_TG_API_HASH=abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890
SIMBRIDGE_AGENT_TOKEN=generate-a-random-uuid-here
SIMBRIDGE_HTTP_SECRET=generate-another-random-uuid-here
SIMBRIDGE_AMI_PASSWORD=YOUR-AMI-PASSWORD
EOF
sudo chmod 0600 /etc/simbridge/env
```

**Generate random secrets:**

```bash
# Agent token — used for internal API auth between agent ↔ userbot
openssl rand -hex 32

# HTTP secret — used for hook script auth (Asterisk → userbot)
openssl rand -hex 32
```

> **Never commit** `/etc/simbridge/env`. It is in `.gitignore`.

---

## Step 4 — Edit Main Configuration

```bash
sudo vim /etc/simbridge/simbridge.yaml
```

For **single-node**, the key changes from the example:

```yaml
node:
  role: all-in-one
  id: gsm-01

agent:
  listen: "127.0.0.1:8090"     # localhost for single-node
  token_env: SIMBRIDGE_AGENT_TOKEN
  allowed_peers: []             # empty for single-node (localhost always allowed)

userbot_http:
  listen: "127.0.0.1:8088"     # localhost for single-node
  secret_env: SIMBRIDGE_HTTP_SECRET
  allowed_peers: []             # empty for single-node

asterisk:
  ami_host: 127.0.0.1
  ami_port: 5038
  ami_username: simbridge
  ami_password_env: SIMBRIDGE_AMI_PASSWORD

telegram:
  master_username: "your_telegram_username"  # your actual Telegram username
  session_path: /var/lib/simbridge/sim_session
  acl_file: /etc/simbridge/acl.conf
```

---

## Step 5 — Add Telegram Users (ACL)

ACL format: `<telegram_user_id> <right1> <right2> ...`

Available rights: `in_sms`, `in_call`, `out_sms`, `out_call`

```bash
sudo tee /etc/simbridge/acl.conf <<'EOF'
# Telegram UID    Rights
123456789         in_sms out_sms in_call out_call
EOF
```

**Find your Telegram UID:**

1. Start the userbot (Step 7) — it will log your UID when you send any message
2. Or use a bot like [@userinfobot](https://t.me/userinfobot) to look up your numeric ID

---

## Step 6 — Configure Hook Script Environment

The Asterisk hook scripts need to know the userbot URL and secret.
Set them in the hook scripts directly or export them for the Asterisk process.

**Option A — Inline (simpler for single-node):**

Edit `/opt/simbridge/scripts/tg-sms-forward.sh` and `/opt/simbridge/scripts/tg-voice-forward.sh`:

```bash
USERBOT_URL="http://127.0.0.1:8088"
USERBOT_SECRET="your-http-secret-from-step-3"
```

**Option B — systemd environment file for Asterisk:**

```bash
echo "USERBOT_URL=http://127.0.0.1:8088" | sudo tee -a /etc/simbridge/env
echo "USERBOT_SECRET=your-http-secret" | sudo tee -a /etc/simbridge/env
```

Then in the systemd override for Asterisk:

```bash
sudo systemctl edit asterisk
# Add:
# [Service]
# EnvironmentFile=/etc/simbridge/env
```

---

## Step 7 — Start Services

```bash
# Order matters: Asterisk first, then agent, then userbot
sudo systemctl restart asterisk
sudo systemctl restart simbridge-agent
sudo systemctl restart simbridge-userbot
```

Check status:

```bash
systemctl status simbridge-agent simbridge-userbot
journalctl -u simbridge-agent -u simbridge-userbot --since '5 min ago' -f
```

### First-Time Telegram Login

On first run, the userbot will create a session file and request phone verification:

```
[INFO] Userbot started, connected to Telegram
[INFO] Phone number needed for login: +7XXXXXXXXXX
[INFO] Code sent via Telegram app
```

Send the code via Telegram (it will prompt you). After authentication, the session is saved to `/var/lib/simbridge/sim_session` and reused on subsequent starts.

---

## Step 8 — Verify

### Health Check

```bash
curl http://127.0.0.1:8090/v1/health | python3 -m json.tool
```

Expected: `{"status": "ok", "components": {"asterisk": "ok", "modem": "ok", ...}}`

### Agent API

```bash
curl -H "Authorization: Bearer $SIMBRIDGE_AGENT_TOKEN" \
     http://127.0.0.1:8090/v1/modem/status
```

### Telegram Commands

Open Telegram, find your account (the one running the userbot), and try:

```
/help          — show available commands (if you have any ACL rights)
```

---

## Step 9 — Test SMS

### Incoming

Send an SMS to the SIM card number from another phone. Check:

1. Asterisk log: `asterisk -rx "core show trace"` or `journalctl`
2. Telegram: message should appear within 3 seconds formatted as:

   ```
   SMS +79161234567 (Contact Name):
   Hello from real phone!
   ```

### Outgoing

In Telegram, send to the userbot:

```
/sms +79161234567 Test message from SimBridge
```

Expected: "Отправлено" → "Доставлено" (delivery report).

---

## Troubleshooting

### Agent won't start

```bash
journalctl -u simbridge-agent --since '2 min ago'
# Common: bind-address rejected (0.0.0.0 not allowed), config syntax error
```

### Userbot can't connect to Telegram

```bash
journalctl -u simbridge-userbot --since '2 min ago'
# Common: wrong API_ID/API_HASH, session file corrupted → delete and re-auth
# Session re-auth: docs/re-auth.md
```

### SMS not forwarded to Telegram

1. Check hook script: `asterisk -rx "logger show"` for script errors
2. Check userbot HTTP server is listening: `ss -tlnp | grep 8088`
3. Test manually:

   ```bash
   curl -X POST http://127.0.0.1:8088/events/sms \
     -H "Content-Type: application/json" \
     -H "X-SimBridge-Secret: YOUR_SECRET" \
     -d '{"phone_number": "+79161234567", "text": "manual test"}'
   ```

### ACL denies all commands

Check your UID is in `/etc/simbridge/acl.conf` with correct rights:

```bash
cat /etc/simbridge/acl.conf
# Reload without restart: send SIGHUP (or restart the service)
```

### Modem not registered

```bash
asterisk -rx "dongle status"
# Check: SIM PIN disabled? Signal strength? Operator name?
```

---

## File Reference

| File | Edit? | Description |
|---|---|---|
| `/etc/simbridge/simbridge.yaml` | Yes | Main config (timings, ports, paths) |
| `/etc/simbridge/env` | Yes | Secrets (API keys, tokens, passwords) |
| `/etc/simbridge/acl.conf` | Yes | Telegram user permissions |
| `/etc/simbridge/blacklist.txt` | Yes | Blocked phone numbers |
| `/etc/asterisk/manager_custom.conf` | Yes | AMI credentials |
| `/etc/asterisk/extensions_custom.conf` | Yes | Dialplan (SMS/voicemail flows) |
| `/var/lib/simbridge/sim_session` | No | Telegram session (auto-created) |
| `/var/log/simbridge/audit.jsonl` | No | Audit log (append-only) |
