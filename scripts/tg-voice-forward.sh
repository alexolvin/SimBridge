#!/bin/bash
set -euo pipefail
# tg-voice-forward.sh — Asterisk hook: forward voicemail to Telegram userbot.
#
# Called from the hangup handler after MixMonitor finishes recording.
# Normalizes volume with ffmpeg loudnorm, then sends as voice note via HTTP.
#
# Environment variables set by Asterisk:
#   MIXMONITORY_FILE — path to the recorded WAV file
#   DonglePhoneNumber — caller phone number

RECORDING="${MIXMONITORY_FILE:-}"
PHONE_NUMBER="${DonglePhoneNumber:-unknown}"
USERBOT_URL="${USERBOT_URL:-http://127.0.0.1:8088}"
USERBOT_SECRET="${USERBOT_SECRET:-}"

if [ -z "$RECORDING" ] || [ ! -f "$RECORDING" ]; then
    logger -t simbridge "WARNING: no recording file found"
    exit 0
fi

# Normalize volume (knowledge item 6 — without loudnorm, Telegram notes are too quiet)
NORM_FILE="${RECORDING%.wav}-norm.wav"
ffmpeg -y -i "$RECORDING" -af "loudnorm=I=-16:LRA=11:TP=-1.5" -ar 44100 -ac 2 \
    -c:a libopus -b:a 128k "${NORM_FILE}.opus" 2>/dev/null || {
    # ffmpeg failed — try ogg vorbis fallback
    ffmpeg -y -i "$RECORDING" -af "loudnorm=I=-16:LRA=11:TP=-1.5" -ar 44100 -ac 2 \
        -c:a libvorbis -b:a 128k "${NORM_FILE}.ogg" 2>/dev/null || {
        logger -t simbridge "ERROR: ffmpeg normalization failed for $RECORDING"
        # Use original file as last resort
        NORM_FILE="$RECORDING"
    }
}

# Determine the final file
FINAL_FILE="${NORM_FILE}.opus"
if [ ! -f "$FINAL_FILE" ]; then
    FINAL_FILE="${NORM_FILE}.ogg"
fi
if [ ! -f "$FINAL_FILE" ]; then
    FINAL_FILE="$RECORDING"
fi

# Send to userbot HTTP server
curl -sf --max-time 30 \
    -X POST \
    -H "X-SimBridge-Secret: ${USERBOT_SECRET}" \
    -F "file=@${FINAL_FILE}" \
    -F "phone_number=${PHONE_NUMBER}" \
    -F "modem_id=gsm" \
    "${USERBOT_URL}/events/voicemail" || {
    logger -t simbridge "ERROR: failed to forward voicemail from ${PHONE_NUMBER}"
}

# Cleanup temp files
rm -f "$NORM_FILE" "${NORM_FILE}.opus" "${NORM_FILE}.ogg" 2>/dev/null || true