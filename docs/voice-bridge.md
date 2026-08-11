# Voice Bridge Architecture (Stage 04)

## Bridge Selection — Research Results (S04.1)

### Candidates Evaluated

Evaluated in order: tg2sip (primary), sip-tg-bridge (fallback), direct ntgcalls (last resort).

#### 1. Infactum/tg2sip — DISQUALIFIED (libtgvoip)

- **Media library**: `libtgvoip` (directory in repo tree). **DISQUALIFIED** per task rule: "any candidate depending on `libtgvoip` rather than `tgcalls`/`ntgcalls`/`WebRTC` is disqualified regardless of how well it appears to work, because it is on a deprecation path."
- **Codecs**: `L16@48000` or `OPUS@48000` — too high for GSM 8kHz ulaw. Would need transcoding on both legs.
- **Build**: CMake, C++17, `settings.ini` with API_ID/API_HASH.
- **Status**: 46 commits, CI workflow, but dead end due to libtgvoip dependency.

#### 2. foobar26/tg2sip (ntgcalls fork) — NOT FOUND

- The task document references this as a "maintained fork of Infactum/tg2sip, rebuilt on ntgcalls."
- Both `https://github.com/foobar26/tg2sip` and `https://github.com/foobar2003/tg2sip` return HTTP 404.
- GitHub search for "tg2sip" returns only Infactum/tg2sip and forks of it.
- **Conclusion**: This fork may be private, deleted, or the username is incorrect. Without access, this candidate cannot be evaluated. **MANUAL_VERIFY** — the next session should confirm whether this repo exists under a different name or is private.

#### 3. blitss/sip-tg-bridge — VIABLE (ntgcalls + LiveKit SIP)

- **Media library**: `ntgcalls` (pytgcalls/ntgcalls at commit 9e4890a). **NOT disqualified.**
- **Architecture**: Based on [LiveKit SIP](https://github.com/livekit/sip) audio pipeline. Adapts LiveKit's SIP/RTP handling and audio transcoding for direct Telegram integration instead of WebRTC rooms.
- **Status**: **POC / Work in Progress** — 7 commits, very young project.
- **Build**: `make build-bridge` or `make build-all` (CMake + Go).
- **Language**: Go binary (`cmd/sip-tg-bridge`) with C++ bridge code and ntgcalls submodule.
- **Assessment**: Promising architecture, but POC status means gaps in error handling, call control, and production hardening.

#### 4. Direct ntgcalls Integration — VIABLE (LAST RESORT)

- **Library**: pytgcalls/ntgcalls — mature Python library with Go bindings.
- **Topics**: audio, calls, cpp, ffmpeg, group-chat, library, python, stream, telegram, tgcalls, video, video-calls, video-chat, voice-chat, voip, webrtc.
- **Go bindings**: Available via CGO. Example in `./examples/go/`.
- **Assessment**: Would give maximum control but requires building the SIP layer ourselves. Only pursue if sip-tg-bridge proves insufficient.

### Selection: blitss/sip-tg-bridge (FALLBACK → PRIMARY by default)

**Reasoning**: The primary candidate (foobar26/tg2sip) is unavailable. sip-tg-bridge uses ntgcalls (not libtgvoip), is built on LiveKit SIP (battle-tested), and is explicitly designed as a tg2sip substitute. The POC status is a risk, but it's the only ntgcalls-based bridge with SIP integration that is publicly available.

**Contingency**: If sip-tg-bridge fails in practice (build issues, call reliability), fall back to direct ntgcalls integration using Go bindings + custom pjsip wrapper.

**MANUAL_VERIFY**: Build sip-tg-bridge from source and place a real Telegram voice call. This cannot be verified in this session without the build environment.

---

## Transport Decision: Plain RTP over Tailscale, no SRTP

### Rationale

Tailscale (WireGuard) provides point-to-point traffic encryption.
Every packet between the two nodes is encrypted at the network layer.
The device's private key never leaves the device — Tailscale cannot
decrypt the traffic either.

Adding SRTP would mean:
- A second key infrastructure (DTLS-SRTP requires certificate exchange)
- Per-packet CPU cost for encrypting already-encrypted traffic
- Certificate management overhead

This is **duplicated mechanism** — refused under Rule 1.

Plain RTP over the tailnet is the correct choice.

### When SRTP becomes necessary

If the transport ever changes to something untrusted (public internet
without WireGuard, shared hosting, etc.), enable `voice.srtp: true`
in the config. The bridge code supports both modes.

## Where the Bridge Runs

On the **Telegram node**, next to the userbot. Telegram traffic
(MTProto + WebRTC voice) terminates there. Only SIP + RTP crosses
the tailnet to the GSM node.

This preserves the geographic separation: Telegram connection from
one location, SIM card physically elsewhere.

## PJSIP Configuration

```ini
[pjsip+tg-bridge]
type=aor
max_contacts=1
qualify_freq=60

[pjsip+tg-bridge]
type=auth
username=tg-bridge
password=bridge-secret
type=user

[tg-bridge]
type=endpoint
aors=pjsip+tg-bridge
auth=pjsip+tg-bridge
context=incoming-mobile
disallow=all
allow=ulaw,alaw
dtmf_mode=rfc2833
direct_media=no
transport=udp
```

## Media Flow

```
Telegram User ──MTProto+WebRTC──► sip-tg-bridge (Telegram node)
                                        │
                                   SIP 5062
                                        │
                                    Tailscale
                                        │
                                   SIP + RTP
                                        │
                              Asterisk (GSM node)
                                        │
                                   chan_dongle
                                        │
                                   GSM Network
```

## Incoming Call Flow — Voicemail as Fallback (S03.4)

### Current (Pre-Stage-04)

Incoming calls go directly to voicemail — there is no Telegram ring yet:

```
chan_dongle (incoming-mobile/s)
    │
    ├── Dial(Local/voicemail-fallback@voicemail-ctx, ${RING_WAIT_SECONDS})
    │
    └── voicemail-ctx/voicemail-fallback
            │
            ├── Gosub(voicemail-record, s, 1)
            │   ├── Answer()
            │   ├── MixMonitor()        ← starts BEFORE prompt (S03.1)
            │   ├── Playback(vm-prompt)
            │   ├── WaitExten()
            │   └── Hangup()
            │
            └── hangup-handler
                ├── MixMonitor callback (recording complete)
                └── tg-voice-forward.sh → /events/voicemail (HTTP)
```

### Post-Stage-04 (Planned)

Stage 04 introduces a state machine that rings the user in Telegram first:

```
chan_dongle (incoming-mobile/s)
    │
    ├── Dial(SIP/tg-bridge@tg-ringing, ${OUTBOUND_RING_TIMEOUT})
    │   │
    │   └── If unanswered → voicemail-ctx/voicemail-fallback (same sub)
    │
    └── If answered → live voice bridge (Stage 04)
```

The voicemail recording sub (`voicemail-record`) is a reusable, stateless branch
that can be invoked from any context. It requires:

- `CALL_FROM` — caller phone number (set by caller context)
- Channel variables from generated globals (`RING_WAIT_SECONDS`, `MAX_RECORD_SECONDS`, `VM_PROMPT`)

### Early Hangup Detection (S03.1)

The `tg-voice-forward.sh` script determines the voicemail type by recording
duration:

| Duration | Type | Telegram Notification |
|---|---|---|
| < 3s | `early_hangup` | "📞 Звонок — (Name)" (no audio) |
| ≥ 3s, valid audio | `normal` | "🎙 Голосовое — (Name)" (with audio) |
| No file found | `recording_missing` | "⚠️ Нет записи — (Name)" |

### Voicemail Recording — Prompt Handling

MixMonitor starts before the prompt playback (S03.1 fix). The resulting
recording contains the prompt at the beginning. This is intentional: it makes
it audible when the call was answered and gives context to the message. The
prompt duration is used by the forwarding script to distinguish early hangups
from actual messages.
