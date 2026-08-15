"""Voicemail recording forward — single implementation (Rule 1).

Used by both trigger paths:
  - scripts/tg-voice-agi.py      (dialplan h-exten: caller hung up
                                   mid-recording, or the record timeout
                                   finished and the h-exten ran)
  - scripts/sweep-recordings.py  (systemd timer: recording left behind
                                   when the h-exten forward could not
                                   run — Asterisk crash, userbot down;
                                   caller is reported as "unknown")

Pipeline (preserved from the old tg-voice-forward.sh, plus fixes):
  1. ffprobe the duration
  2. classify: missing file -> "recording_missing"; zero audio
     (duration 0) -> "early_hangup" reported as JSON (a 0-second voice
     note is useless in Telegram); duration < early_hangup_max_seconds
     -> "early_hangup" (S03.1); else "normal"
  3. ffmpeg loudnorm (I=-16:LRA=11:TP=-1.5, 48000 Hz mono, libopus 32k
     — production parity with tg-voice-forward.sh; knowledge item 6:
     Telegram voice notes are too quiet without normalization),
     fallback libvorbis/ogg, fallback the original file
  4. multipart POST /events/voicemail to the userbot with the
     X-SimBridge-Secret header
  5. cleanup is the CALLER's decision (AGI and sweeper both delete
     the recording on success, keep it on failure so the next
     trigger can retry)

Stdlib only: runs under both the agent venv and Asterisk's system
python3. All subprocess calls use argument vectors — no shell.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

DEFAULT_EARLY_HANGUP_MAX_SECONDS = 3
HTTP_TIMEOUT = 30.0
PROBE_TIMEOUT = 15.0
ENCODE_TIMEOUT = 120.0

# loudnorm recipe (knowledge item 6): normalize speech to -16 LUFS
LOUDNORM = "loudnorm=I=-16:LRA=11:TP=-1.5"
# Audio params: production parity with tg-voice-forward.sh
# (libopus 32k, 48000 Hz, mono — standard Telegram voice note shape)
AUDIO_RATE = "48000"
AUDIO_CHANNELS = "1"
AUDIO_BITRATE = "32k"


def log(msg: str) -> None:
    """Log to stderr (Asterisk log / journald, depending on the caller)."""
    print(msg, file=sys.stderr, flush=True)


def ffprobe_duration(path: str) -> float:
    """Recording duration in seconds (0.0 on any failure)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        )
        return float(r.stdout.strip() or "0")
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def classify(duration: float, early_hangup_max_seconds: int) -> str:
    """S03.1: a recording shorter than the threshold means the caller
    hung up right after the greeting."""
    if int(duration) < early_hangup_max_seconds:
        return "early_hangup"
    return "normal"


def _ffmpeg(input_path: str, codec: str, output_path: str) -> bool:
    """ffmpeg loudnorm to *output_path* with *codec* (arg vector, no shell)."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-af", LOUDNORM,
             "-ar", AUDIO_RATE, "-ac", AUDIO_CHANNELS,
             "-c:a", codec, "-b:a", AUDIO_BITRATE, output_path],
            capture_output=True, timeout=ENCODE_TIMEOUT,
        )
        return r.returncode == 0 and os.path.isfile(output_path)
    except (OSError, subprocess.SubprocessError):
        return False


def normalize_volume(input_path: str, temp_files: list[str]) -> str:
    """loudnorm → opus; fallback ogg; fallback the original file.

    Appends the temp file paths to *temp_files* (caller cleans them up).
    """
    base = input_path[:-4] if input_path.endswith(".wav") else input_path
    opus, ogg = base + ".opus", base + ".ogg"
    temp_files.extend((opus, ogg))
    if _ffmpeg(input_path, "libopus", opus):
        return opus
    if _ffmpeg(input_path, "libvorbis", ogg):
        return ogg
    log(f"ERROR: ffmpeg normalization failed for {input_path}, sending raw")
    return input_path


def _post(url: str, secret: str, path: str, data: bytes,
          content_type: str) -> tuple[bool, str]:
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=data,
        headers={"Content-Type": content_type, "X-SimBridge-Secret": secret},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except (urllib.error.URLError, OSError) as e:
        return False, str(e)


def post_json(url: str, secret: str, path: str, payload: dict) -> tuple[bool, str]:
    return _post(
        url, secret, path,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        "application/json",
    )


def post_multipart(url: str, secret: str, path: str, fields: dict,
                   file_field: str, filepath: str) -> tuple[bool, str]:
    """Build a multipart/form-data body by hand (stdlib has no builder)."""
    boundary = "----simbridge" + uuid.uuid4().hex
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
            f'{value}\r\n'.encode("utf-8")
        )
    with open(filepath, "rb") as fh:
        file_data = fh.read()
    parts.append(
        (f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
         f'filename="{os.path.basename(filepath)}"\r\nContent-Type: audio/ogg\r\n\r\n')
        .encode("utf-8") + file_data + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return _post(url, secret, path, b"".join(parts),
                 f"multipart/form-data; boundary={boundary}")


def _report_missing_or_empty(recording_path: str, caller: str,
                             correlation: str, url: str, secret: str,
                             duration: float,
                             vm_type: str) -> tuple[bool, str, str]:
    """JSON-only voicemail event (no file): missing file, or a recording
    with zero audio (caller hung up before speaking)."""
    log(f"WARNING: {vm_type} for {correlation} "
        f"(file={'present' if os.path.isfile(recording_path) else 'absent'}, "
        f"duration={duration}s)")
    ok, detail = post_json(url, secret, "/events/voicemail", {
        "phone_number": caller,
        "voicemail_type": vm_type,
        "correlation_id": correlation,
        "duration": str(duration),
    })
    if not ok:
        log(f"ERROR: failed to report {vm_type} for {correlation}: {detail}")
    return ok, detail, vm_type


def forward_recording(
    recording_path: str,
    caller: str,
    correlation: str,
    url: str,
    secret: str,
    early_hangup_max_seconds: int = DEFAULT_EARLY_HANGUP_MAX_SECONDS,
) -> tuple[bool, str, str]:
    """Forward one voicemail recording to the userbot.

    Returns (ok, detail, voicemail_type). Does NOT delete the recording
    or the temp files — the caller decides (both current callers delete
    on success, keep on failure so the next trigger retries).
    """
    if not os.path.isfile(recording_path):
        return _report_missing_or_empty(
            recording_path, caller, correlation, url, secret,
            duration=0.0, vm_type="recording_missing",
        )

    duration = ffprobe_duration(recording_path)

    # Zero audio: the file exists but has no sound (caller hung up
    # before speaking). Report as JSON — never send a 0-second note.
    if duration <= 0.0:
        return _report_missing_or_empty(
            recording_path, caller, correlation, url, secret,
            duration=0.0, vm_type="early_hangup",
        )

    vm_type = classify(duration, early_hangup_max_seconds)
    log(f"Voicemail from {caller}: type={vm_type} duration={duration}s")

    temp_files: list[str] = []
    try:
        final_file = normalize_volume(recording_path, temp_files)
        ok, detail = post_multipart(
            url, secret, "/events/voicemail",
            fields={
                "phone_number": caller,
                "voicemail_type": vm_type,
                "correlation_id": correlation,
                "duration": str(duration),
            },
            file_field="file",
            filepath=final_file,
        )
        if not ok:
            log(f"ERROR: failed to forward voicemail from {caller}: {detail}")
        return ok, detail, vm_type
    finally:
        for f in temp_files:
            try:
                os.unlink(f)
            except OSError:
                pass


def cleanup_recording(recording_path: str) -> None:
    """Delete the original recording (consumed by a successful forward)."""
    try:
        os.unlink(recording_path)
    except OSError:
        pass
