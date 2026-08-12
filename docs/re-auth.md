# Re-Auth Procedure — Telegram Session Invalidation

## When this is needed

Telegram session (`*.session`) becomes invalid when:
- Telegram forces re-auth (security policy, prolonged inactivity)
- Session file is corrupted or deleted
- Account password/2FA was changed from another device
- Telegram detects suspicious activity

## Detection

The userbot logs an error on startup:
```
ERROR: Telegram session invalid — Telethon session error
```

The health endpoint reports `telegram_session: healthy=false`.

## Recovery procedure

1. **Stop the userbot service**:
   ```bash
   systemctl stop simbridge-userbot
   ```

2. **Back up the old session** (for debugging):
   ```bash
   mv /var/lib/simbridge/sim_session.session /var/lib/simbridge/sim_session.session.bak.$(date +%Y%m%d)
   mv /var/lib/simbridge/sim_session.session-journal /var/lib/simbridge/sim_session.session-journal.bak.$(date +%Y%m%d) 2>/dev/null || true
   ```

3. **Remove the old session files**:
   ```bash
   rm -f /var/lib/simbridge/sim_session.session /var/lib/simbridge/sim_session.session-journal
   ```

4. **Start the userbot** — it will prompt for phone number and 2FA:
   ```bash
   systemctl start simbridge-userbot
   journalctl -u simbridge-userbot -f  # watch the logs
   ```

5. **Follow the phone verification**:
   - Telegram will send a code to the account (or linked devices)
   - The code is entered via the journal log (Telethon prompts on stdout/stderr)
   - If 2FA is enabled, enter the password when prompted

6. **Verify the new session**:
   ```bash
   curl -s http://127.0.0.1:8088/health | python3 -m json.tool
   ```
   Should show `telegram_session: healthy=true`.

7. **Restart dependent services**:
   ```bash
   systemctl restart simbridge-agent
   ```

## Prevention

- Keep the session file backed up (encrypted):
  ```bash
  gpg --symmetric --cipher-algo AES256 /var/lib/simbridge/sim_session.session
  ```
- Monitor the health endpoint for session status
- Alert on `telegram_session_invalid` (configured in `core/alerting.py`)
