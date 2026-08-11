# Troubleshooting

## Hard-Won Knowledge

These cost real debugging time on the original deployment.

### 1. DongleSendSMS — quote the text

SMS text must always be quoted, or commas truncate the message.
In the AMI client, the text is passed as a structured field — this
is handled automatically. If using the shell directly:
```bash
DongleSendSMS(gsm,+79161234567,'message with, commas')
```

### 2. Delivery reports

Status variables (`% DongleDlrStatus`) are frequently empty.
The arrival of the `report` extension itself is the signal,
not its contents. A delivery report arrived = message delivered.

### 3. Post-hangup processing

Use `MixMonitor` + `hangup_handler`, not `Record`.
`Record` stops when the channel hangs up; `MixMonitor` continues
recording the file and the hangup handler processes it after.

### 4. Sound file paths

Absolute paths are more reliable than `custom/...` relative references.
Always use absolute paths in `extensions.conf`.

### 5. Audio format

Prompt audio must be 8 kHz mono, ulaw or wav format.
Telegram voice notes are processed with `ffmpeg loudnorm` —
without it, voice notes are too quiet on the receiving end.

### 6. Python venv

The userbot MUST run from its venv interpreter, not system `python3`.
The systemd unit uses `/opt/simbridge-venv/bin/python`.

### 7. Tailscale hostnames

Use MagicDNS FQDN or the raw IP. Short hostnames may not resolve.
Never use `hostname` — use the full MagicDNS name or IP.

### 8. MASTER_ID resolution

Always resolve master user ID via `get_entity` at startup,
never a hardcoded stale ID (knowledge item 10).

### 9. Modem movement

Move the USB modem to a new node ONLY after that node's
configuration is fully ready (knowledge item 11).

### 10. dongle.conf context

`context = incoming-mobile` is what routes calls, SMS, USSD, and
delivery reports into one context — the `s`, `sms`, `report`,
`ussd` extensions all live there.

## Common Issues

### Agent won't connect to Asterisk AMI

```
ERROR: AMI login failed: {'Response': 'Error'}
```

- Check `/etc/asterisk/manager.conf` — username and password must match
- Check that AMI is enabled: `enabled = yes`
- Check bind address: should be `127.0.0.1` for local agent
- Restart Asterisk: `systemctl restart asterisk`

### Userbot can't login to Telegram

```
ERROR: Could not connect to Telegram
```

- Check API_ID and API_HASH in /etc/simbridge/env
- Session file may be corrupted: delete `/var/lib/simbridge/sim_session.*`
  and restart (will re-authenticate)
- Check network: userbot needs outbound HTTPS to Telegram

### SMS not arriving on Telegram

- Check hook script is called: `journalctl -u asterisk | grep tg-sms`
- Check userbot HTTP is reachable: `curl http://localhost:8088/health`
- Check ACL: user must have `in_sms` right
- Check blacklist: number may be blocked

### SMS text truncated

If commas appear as truncation points, the shell hook is not
URL-encoding the text. Check `tg-sms-forward.sh` — it should
URL-encode before sending JSON.

### Rate limiting

If you see "Rate limit exceeded" in Telegram:
- Check `limits.sms_per_hour` in config
- The limit is per-user, per-hour
- Current count: check audit log for `SMS_SEND_REQUESTED` events