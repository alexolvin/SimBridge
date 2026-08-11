#!/bin/bash
set -euo pipefail
# tg-voice-forward.sh — Asterisk hook: forward voicemail to Telegram userbot.
#
# Called from the hangup handler after MixMonitor finishes recording.
# Normalizes volume with ffmpeg loudnorm, detects early hangup,
# then sends as voice note via HTTP.
#
# Arguments (passed from dialplan):
#   $1 — CORRELATION (Asterisk UNIQUEID)
#   $2 — CALL_FROM (caller phone number)
#   $3 — VMFILE (full path to recording WAV, optional)
#
# S03.1: Distinguish early hangup vs normal voicemail by recording duration.
# S03.3: Cleanup temp files on both success AND failure paths.

CORRELATION="${1:-unknown}"
PHONE_NUMBER="${2:-unknown}"
VMFILE="${3:-${VM_RECORDINGS_DIR:-/var/lib/simbridge/recordings}/${CORRELATION}.wav}"
USERBOT_URL="${USERBOT_URL:-http://127.0.0.1:8088}"
USERBOT_SECRET="${USERBOT_SECRET:-}"

# --- Failure-safe temp file tracking (S03.3) ---
TEMP_FILES=()
cleanup() {
    for f in "${TEMP_FILES[@]+"${TEMP_FILES[@]}"}"; do
        rm -f "$f" 2>/dev/null || true
    done
    # Always remove original recording after processing (success or failure)
    if [ -n "$VMFILE" ] && [ -f "$VMFILE" ]; then
        rm -f "$VMFILE" 2>/dev/null || true
    fi
}
trap cleanup EXIT

if [ ! -f "$VMFILE" ]; then
    logger -t simbridge "WARNING: no recording file found for ${CORRELATION}"
    # Notify Telegram about missing recording
    curl -sf --max-time 10 \
        -X POST \
        -H "X-SimBridge-Secret: ${USERBOT_SECRET}" \
        -H "Content-Type: application/json" \
        -d "{\"phone_number\": \"${PHONE_NUMBER}\", \"voicemail_type\": \"recording_missing\", \"correlation_id\": \"${CORRELATION}\"}" \
        "${USERBOT_URL}/events/voicemail" 2>/dev/null || true
    exit 0
fi

# S03.1: Detect early hangup by recording duration
# If duration < prompt duration threshold (3s), caller hung up during greeting
DURATION_SECS=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$VMFILE" 2>/dev/null || echo "0")
# Compare integer seconds (ffprobe returns float, truncate for comparison)
DURATION_INT=${DURATION_SECS%.*}
if [ -z "$DURATION_INT" ] || [ "$DURATION_INT" = "" ]; then
    DURATION_INT=0
fi

# Prompt is typically 5-10 seconds. If recording is < 3s, it's an early hangup.
VM_TYPE="normal"
if [ "$DURATION_INT" -lt 3 ] 2>/dev/null; then
    VM_TYPE="early_hangup"
fi

logger -t simbridge "Voicemail from ${PHONE_NUMBER}: type=${VM_TYPE} duration=${DURATION_SECS}s"

# Normalize volume with ffmpeg loudnorm (knowledge item 6)
NORM_BASE="${VMFILE%.wav}-norm"
TEMP_FILES+=("$NORM_BASE" "${NORM_BASE}.opus" "${NORM_BASE}.ogg")

if ffmpeg -y -i "$VMFILE" -af "loudnorm=I=-16:LRA=11:TP=-1.5" -ar 44100 -ac 2 \
    -c:a libopus -b:a 128k "${NORM_BASE}.opus" 2>/dev/null; then
    FINAL_FILE="${NORM_BASE}.opus"
elif ffmpeg -y -i "$VMFILE" -af "loudnorm=I=-16:LRA=11:TP=-1.5" -ar 44100 -ac 2 \
    -c:a libvorbis -b:a 128k "${NORM_BASE}.ogg" 2>/dev/null; then
    FINAL_FILE="${NORM_BASE}.ogg"
else
    logger -t simbridge "ERROR: ffmpeg normalization failed for $VMFILE, sending raw"
    FINAL_FILE="$VMFILE"
fi

# Send to userbot HTTP server
curl -sf --max-time 30 \
    -X POST \
    -H "X-SimBridge-Secret: ${USERBOT_SECRET}" \
    -F "file=@${FINAL_FILE}" \
    -F "phone_number=${PHONE_NUMBER}" \
    -F "voicemail_type=${VM_TYPE}" \
    -F "correlation_id=${CORRELATION}" \
    -F "duration=${DURATION_SECS}" \
    "${USERBOT_URL}/events/voicemail" || {
    logger -t simbridge "ERROR: failed to forward voicemail from ${PHONE_NUMBER}"
}

# cleanup() in EXIT trap handles temp file removal (success AND failure)
