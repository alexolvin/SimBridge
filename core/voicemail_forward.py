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
     (duration 0) -> "early_hangup" reported text-only (a 0-second
     voice note is useless in Telegram); speech time (recording duration
     minus the greeting, S03.1) < early_hangup_max_seconds ->
     "early_hangup" reported as TEXT ONLY — by definition the audio is
     a greeting fragment plus under-threshold silence, there is no
     caller content in it; else "normal"
  3. ffmpeg loudnorm, two-pass (measure then apply; I=-14:LRA=11:
     TP=-1.5, 48000 Hz mono, libopus 32k — production parity with
     tg-voice-forward.sh; knowledge item 6: Telegram voice notes are
     too quiet without normalization; -14 is the "loud voice note"
     target, single-pass is the fallback when measurement fails),
     fallback libvorbis/ogg, fallback the original file. The greeting
     is trimmed from the front (S03.1: MixMonitor starts before
     Playback, so the prompt is captured at the head of the WAV) —
     stated choice: trim, not accept
  4. POST /events/voicemail to the userbot — multipart form-data
     (with audio) or urlencoded (text-only events) — with the
     X-SimBridge-Secret header. The handler parses both with
     req.form(); a JSON body would parse to an EMPTY form and be
     silently misdelivered (live incident 2026-08-21).
  5. cleanup is the CALLER's decision (AGI and sweeper both delete
     the recording on success, keep it on failure so the next
     trigger can retry)

Stdlib only: runs under both the agent venv and Asterisk's system
python3. All subprocess calls use argument vectors — no shell.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_EARLY_HANGUP_MAX_SECONDS = 3
HTTP_TIMEOUT = 30.0
PROBE_TIMEOUT = 15.0
ENCODE_TIMEOUT = 120.0

# loudnorm recipe (knowledge item 6): normalize speech to a loud,
# consistent voice-note level. Target -14 LUFS, not the -16 default:
# -16 was measured correctly on the wire but reported "very quiet" on
# the phone (2026-08-21); -14 is the documented "louder" streaming
# target. TP=-1.5 keeps clipping headroom, LRA=11 keeps dynamics.
LOUDNORM_TARGET_I = "-14"
LOUDNORM_TARGET_LRA = "11"
LOUDNORM_TARGET_TP = "-1.5"
LOUDNORM = (
    f"loudnorm=I={LOUDNORM_TARGET_I}"
    f":LRA={LOUDNORM_TARGET_LRA}:TP={LOUDNORM_TARGET_TP}"
)
# Integrated loudness (dB) at/below which the content is digital
# silence / noise floor — the measured values would be unusable, so the
# two-pass path falls back to single-pass instead of amplifying hiss.
LOUDNORM_SILENCE_FLOOR = -70.0
# Audio params: production parity with tg-voice-forward.sh
# (libopus 32k, 48000 Hz, mono — standard Telegram voice note shape)
AUDIO_RATE = "48000"
AUDIO_CHANNELS = "1"
AUDIO_BITRATE = "32k"


def log(msg: str) -> None:
    """Log to stderr (Asterisk log / journald, depending on the caller)."""
    print(msg, file=sys.stderr, flush=True)


# Raw Asterisk sound formats: ffprobe cannot infer these from content,
# an explicit -f hint is required. Asterisk raw sounds are 8 kHz mono
# (the native rate of the dialplan sounds dir). One probe function
# shared by the AGI, the sweeper and the config generator (Rule 1).
_RAW_FORMAT_HINTS = {
    ".ulaw": ("-f", "mulaw", "-ar", "8000", "-channel_layout", "mono"),
    ".gsm": ("-f", "gsm", "-ar", "8000", "-channel_layout", "mono"),
    ".sln": ("-f", "sln", "-ar", "8000", "-channel_layout", "mono"),
    ".slin": ("-f", "sln", "-ar", "8000", "-channel_layout", "mono"),
    ".alaw": ("-f", "alaw", "-ar", "8000", "-channel_layout", "mono"),
}


def ffprobe_duration(path: str) -> float:
    """Recording duration in seconds (0.0 on any failure).

    Asterisk raw sound files (.ulaw/.gsm/.sln/.alaw) are probed with a
    format hint derived from the extension; container formats (.wav,
    .ogg, ...) are probed as-is.
    """
    hint = _RAW_FORMAT_HINTS.get(os.path.splitext(path)[1].lower(), ())
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", *hint, path],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        )
        return float(r.stdout.strip() or "0")
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def classify(duration: float, prompt_duration: float,
             early_hangup_max_seconds: int) -> str:
    """S03.1: the recording includes the greeting (MixMonitor starts
    before Playback), so only the audio AFTER the prompt counts as the
    caller's message: speech = duration - prompt_duration.

    ``prompt_duration == 0`` reduces to the legacy check
    (``int(duration) < max``), so callers that do not know the prompt
    length keep the old behavior.
    """
    speech = duration - prompt_duration
    if int(speech) < early_hangup_max_seconds:
        return "early_hangup"
    return "normal"


def _ffmpeg(input_path: str, codec: str, output_path: str,
            seek_seconds: float = 0.0, af: str = LOUDNORM) -> bool:
    """ffmpeg loudnorm to *output_path* with *codec* (arg vector, no shell).

    ``seek_seconds`` input-seeks before reading — used to trim the
    greeting from the front of the recording (S03.1). ``af`` is the
    filter chain (single- or two-pass loudnorm, see _loudnorm_filter).
    """
    cmd = ["ffmpeg", "-y"]
    if seek_seconds > 0:
        cmd += ["-ss", f"{seek_seconds:.3f}"]
    cmd += ["-i", input_path,
            "-af", af,
            "-ar", AUDIO_RATE, "-ac", AUDIO_CHANNELS,
            "-c:a", codec, "-b:a", AUDIO_BITRATE, output_path]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=ENCODE_TIMEOUT)
        return r.returncode == 0 and os.path.isfile(output_path)
    except (OSError, subprocess.SubprocessError):
        return False


def _measure_loudness(path: str, seek_seconds: float = 0.0) -> dict | None:
    """Two-pass loudnorm, measurement pass: run the filter to null and
    parse the JSON it prints to stderr.

    Returns ``{"input_i", "input_tp", "input_lra", "offset"}`` as
    floats, or None when ffmpeg fails or the JSON is missing — the
    caller then keeps the single-pass recipe.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostats"]
    if seek_seconds > 0:
        cmd += ["-ss", f"{seek_seconds:.3f}"]
    cmd += ["-i", path,
            "-af", LOUDNORM + ":print_format=json",
            "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=ENCODE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    for block in re.findall(r"\{[^{}]*\}", r.stderr):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if "input_i" not in data:
            continue
        # The JSON key is "target_offset" (the filter's apply-pass
        # option is named "offset" — the two are easy to mix up).
        try:
            return {
                "input_i": float(data["input_i"]),
                "input_tp": float(data["input_tp"]),
                "input_lra": float(data["input_lra"]),
                "offset": float(data["target_offset"]),
            }
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _loudnorm_filter(measured: dict | None) -> str:
    """Filter chain for the apply pass: the plain single-pass recipe,
    or the measured two-pass variant (accurate for dynamic speech).

    Two-pass is used only with finite, usable measurements; ``linear``
    (constant gain) is clipping-free only when the input true peak is
    already below the target true peak.
    """
    if measured is None:
        return LOUDNORM
    values = (measured["input_i"], measured["input_lra"], measured["input_tp"])
    if (not all(math.isfinite(v) for v in values)
            or measured["input_i"] <= LOUDNORM_SILENCE_FLOOR):
        return LOUDNORM
    linear = measured["input_tp"] < float(LOUDNORM_TARGET_TP)
    return (f"{LOUDNORM}:"
            f"measured_I={measured['input_i']:.2f}:"
            f"measured_LRA={measured['input_lra']:.2f}:"
            f"measured_TP={measured['input_tp']:.2f}:"
            f"offset={measured['offset']:.2f}:"
            f"linear={'true' if linear else 'false'}")


def _log_loudness(input_path: str, measured: dict | None,
                  output_path: str) -> None:
    """Log measured input and actual output loudness — the evidence
    line for "is the sent voice note loud enough" (Rule 2)."""
    out = _measure_loudness(output_path)
    if measured is not None and out is not None:
        log(
            f"Loudness {input_path}: in I={measured['input_i']:.1f} "
            f"TP={measured['input_tp']:.1f} -> out I={out['input_i']:.1f} "
            f"TP={out['input_tp']:.1f}"
        )


def normalize_volume(input_path: str, temp_files: list[str],
                     prompt_duration: float = 0.0) -> str:
    """loudnorm → opus; fallback ogg; fallback the original file.

    Appends the temp file paths to *temp_files* (caller cleans them up).
    With ``prompt_duration > 0`` the first that many seconds (the
    greeting, captured because MixMonitor runs before Playback) are
    trimmed off (S03.1).
    """
    base = input_path[:-4] if input_path.endswith(".wav") else input_path
    opus, ogg = base + ".opus", base + ".ogg"
    temp_files.extend((opus, ogg))
    # Two-pass: measure first (on the same trimmed span), then apply
    # with the measured values — accurate for dynamic speech, where
    # single-pass can miss the target by a few dB.
    measured = _measure_loudness(input_path, seek_seconds=prompt_duration)
    filt = _loudnorm_filter(measured)
    if _ffmpeg(input_path, "libopus", opus,
               seek_seconds=prompt_duration, af=filt):
        _log_loudness(input_path, measured, opus)
        return opus
    if _ffmpeg(input_path, "libvorbis", ogg,
               seek_seconds=prompt_duration, af=filt):
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


def post_form(url: str, secret: str, path: str, fields: dict) -> tuple[bool, str]:
    """POST text-only event fields as urlencoded form data.

    The userbot handler parses every voicemail event with
    ``req.form()`` — one mechanism: multipart/form-data (with audio)
    or urlencoded (without). A JSON body parses to an EMPTY form
    there, so every field silently falls back to its default (live
    incident 2026-08-21: an early_hangup event was delivered as
    "normal" from "unknown" with no audio)."""
    return _post(
        url, secret, path,
        urllib.parse.urlencode(fields).encode("utf-8"),
        "application/x-www-form-urlencoded",
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
    """Text-only voicemail event (no file, no audio): missing file,
    or a recording with zero audio (caller hung up before speaking)."""
    log(f"WARNING: {vm_type} for {correlation} "
        f"(file={'present' if os.path.isfile(recording_path) else 'absent'}, "
        f"duration={duration}s)")
    ok, detail = post_form(url, secret, "/events/voicemail", {
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
    prompt_duration: float = 0.0,
) -> tuple[bool, str, str]:
    """Forward one voicemail recording to the userbot.

    ``prompt_duration`` is the greeting length in seconds — the
    recording includes it (S03.1: MixMonitor starts before Playback).
    It is subtracted for classification and trimmed off the sent audio.

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

    vm_type = classify(duration, prompt_duration, early_hangup_max_seconds)
    log(f"Voicemail from {caller}: type={vm_type} duration={duration}s "
        f"(prompt={prompt_duration:.3f}s)")

    # S03.1 (stated choice): an early hangup is, by definition, a
    # greeting fragment plus under-threshold silence — there is no
    # caller content in the audio. Send a text-only "call came in"
    # notification instead of a 1-2 second Telegram note.
    if vm_type == "early_hangup":
        return _report_missing_or_empty(
            recording_path, caller, correlation, url, secret,
            duration=duration, vm_type="early_hangup",
        )

    temp_files: list[str] = []
    try:
        # S03.1: trim the greeting captured at the front of the WAV
        # (stated choice: trim, not accept) so the voice note starts
        # with the caller's words.
        final_file = normalize_volume(
            recording_path, temp_files, prompt_duration=prompt_duration,
        )
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
