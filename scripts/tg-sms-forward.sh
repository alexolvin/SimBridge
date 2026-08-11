#!/bin/bash
set -euo pipefail
# tg-sms-forward.sh — Asterisk hook: forward incoming SMS to Telegram userbot via HTTP.
#
# Called from extensions.conf (sms extension) when chan_dongle receives an SMS.
# Replaces the old SSH reverse path with HTTP POST over Tailscale.
#
# Environment variables set by Asterisk:
#   DonglePhoneNumber — sender phone number
#   DongleText — SMS text (may contain commas, quotes, special chars)
#
# Security: text is URL-encoded and passed as JSON — never shell-interpolated.

# Config (from simbridge.yaml, read at install time)
USERBOT_URL="${USERBOT_URL:-http://127.0.0.1:8088}"
USERBOT_SECRET="${USERBOT_SECRET:-}"

PHONE_NUMBER="${DonglePhoneNumber:-unknown}"
TEXT="${DongleText:-}"

if [ -z "$TEXT" ]; then
    exit 0
fi

# URL-encode the text (commas, quotes, spaces — all safe in JSON)
# Use Python for reliable encoding (available on all target systems)
JSON_PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'phone_number': sys.argv[1],
    'text': sys.argv[2],
    'modem_id': 'gsm'
}))
" "$PHONE_NUMBER" "$TEXT")

# Send to userbot HTTP server (over Tailscale or localhost)
curl -sf --max-time 10 \
    -X POST \
    -H "Content-Type: application/json" \
    -H "X-SimBridge-Secret: ${USERBOT_SECRET}" \
    -d "$JSON_PAYLOAD" \
    "${USERBOT_URL}/events/sms" || {
    # curl failed — log and continue (don't block Asterisk)
    logger -t simbridge "ERROR: failed to forward SMS from ${PHONE_NUMBER}"
}