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

Incoming calls go directly to voicemail — there is no Telegram ring yet.
The voicemail path is a **named, same-context branch** `voicemail` inside
`[incoming-mobile]`, entered with `Goto(voicemail,1)`:

```
chan_dongle (incoming-mobile/s)
    │
    ├── AGI(tg-blacklist-agi.py)          → BL_BLOCKED → Busy(5) (fail-open)
    ├── Set(FWD_URL / MODEM_ID / EH_MAX / VM_PROMPT_DURATION)
    ├── AGI(tg-sms-agi.py, ring)          → userbot /events/sms ("📞 Входящий звонок")
    ├── Wait(${RING_WAIT_SECONDS})        ← ringback on the line
    │
    └── Goto(voicemail, 1)                ← S03.4 named fallback branch
            │
            ├── Answer()
            ├── Set(VMFILE=${VM_REC_DIR}/vm-${UNIQUEID}.wav)
            ├── MixMonitor(${VMFILE})     ← starts BEFORE the prompt (S03.1)
            ├── Playback(${VM_PROMPT})
            ├── Wait(${MAX_RECORD_SECONDS})
            ├── StopMixMonitor()
            └── Hangup()
                    │
                    └── h-exten (same context)
                        ├── StopMixMonitor()   (synchronous WAV finalization)
                        └── AGI(tg-voice-agi.py) → core.voicemail_forward
                            → userbot /events/voicemail
```

Why a same-context named exten and not a separate context: the h-exten
resolves in the channel's *current* context, so a voicemail path in a
different context would need its own h-exten — a second forwarding
mechanism (Rule 1). `Goto(voicemail,1)` keeps everything in
`[incoming-mobile]`; the Stage 04 state machine will call the same target.

### Post-Stage-04 (Planned)

Stage 04 inserts a Telegram ring between the line ringback and the voicemail
fallback — the voicemail branch itself is unchanged:

```
chan_dongle (incoming-mobile/s)
    │
    ├── AGI(tg-blacklist-agi.py)
    ├── AGI(tg-sms-agi.py, ring)          → Telegram ring (in_call audience)
    ├── Wait(${RING_WAIT_SECONDS})        ← the Telegram ring window
    │
    ├── User accepted in Telegram → live voice bridge (Stage 04)
    ├── User rejected / no answer → Goto(voicemail, 1)   ← same branch
    └── No Telegram user at all    → Goto(voicemail, 1)
```

The state machine (`core/call_control.py`) decides; `voicemail,1` is the
single, unchanged fallback target.

### Voicemail Types (S03.1)

The AGI path (`tg-voice-agi.py` → `core/voicemail_forward.py`) classifies by
**speech time** — the recording includes the greeting (MixMonitor starts
before Playback), so speech = duration − PROMPT_DURATION (probed by the
config generator, published as the `PROMPT_DURATION` global):

| Condition | Type | Telegram Notification |
|---|---|---|
| no recording file | `recording_missing` | text-only: "⚠️ Нет записи — (name)" |
| zero audio (0 s) | `early_hangup` | text-only: "📞 Звонок — (name)" |
| speech < `EARLY_HANGUP_MAX_SECONDS` (default 3 s) | `early_hangup` | **text-only** "📞 Звонок — (name)" — the audio is a greeting fragment plus <3 s of silence, no caller content (stated choice) |
| speech ≥ `EARLY_HANGUP_MAX_SECONDS` | `normal` | "🎙 Голосовое — (name)" + voice note, greeting **trimmed off** (stated choice: trim, not accept) |

Delivery goes to the `in_call` audience (a voicemail is a voice event, the
same audience as the ring notification); the label text carries the caller
number + resolved contact name and is sent before the voice note.

### Prompt-in-Recording (S03.1)

MixMonitor starts before Playback, so the greeting is at the front of the
WAV. Chosen handling: **trim** — the forward input-seeks
`PROMPT_DURATION` seconds in the same single ffmpeg pass that applies
loudnorm, so the voice note starts with the caller's words. Rationale: the
user hears only the message; ffmpeg is already a hard dependency (loudnorm);
deterministic given a probed prompt length. If the prompt file is missing or
ffprobe is unavailable, `PROMPT_DURATION` is 0.000 and the behavior degrades
to the legacy one (no trim, full-duration classification) — the generator
logs a warning.

### Cleanup (S03.3)

- **GSM node**: a successfully forwarded recording is deleted by the AGI /
  sweeper; a failed forward keeps it for retry, up to
  `asterisk.sweep_max_retain_seconds` (default 7 days), after which the
  sweeper deletes it — a failed send does not live on disk forever.
- **Telegram node**: the uploaded audio is written to a temp file for the
  voice note and deleted in a `finally` — on the success **and** the failure
  path.

---

## Distributed Mode (S04.4)

### Config-Only Change

Switching from single-node to distributed mode is a **config-only change**.
No code changes are required. The single-node codebase reads all network
parameters from `simbridge.yaml` — no hardcoded assumptions about
`127.0.0.1`.

**Single-node config:**
```yaml
voice:
  bridge_host: 127.0.0.1
  bridge_port: 5062
```

**Distributed config:**
```yaml
voice:
  bridge_host: 100.x.x.x  # Tailscale IP of the Telegram node
  # or: bridge_host: my-telegram-node.tail-<netid>.ts.net  # MagicDNS FQDN
  bridge_port: 5062
```

That is the only code-relevant difference. All other changes are in
Asterisk PJSIP configuration (local_net, external_media_addr).

### Addressing: MagicDNS FQDN or Raw Tailscale IP

Use either the full MagicDNS FQDN (`my-node.tail-<netid>.ts.net`) or the raw
Tailscale IP (`100.x.x.x`). **Never use short hostnames** — they may not
resolve reliably across all nodes in the tailnet.

### SRTP Rationale — Why Plain RTP on Tailscale

Tailscale (WireGuard) provides point-to-point encryption at the network layer.
Every packet between nodes is encrypted end-to-end. The device's private key
never leaves the device — Tailscale cannot decrypt the traffic either.

Adding SRTP would create a **duplicated mechanism** (Rule 1):
- A second key infrastructure (DTLS-SRTP requires certificate exchange)
- Per-packet CPU cost for encrypting already-encrypted traffic
- Certificate management overhead

Plain RTP over the tailnet is the deliberate, correct choice. It is not an
oversight — the tailnet already provides the encryption that SRTP would add.

**When SRTP becomes necessary:** If the transport ever changes to something
untrusted (public internet without WireGuard, shared hosting, etc.), enable
`voice.srtp: true` in the config.

### PJSIP local_net and NAT Settings

For the distributed mode, the PJSIP endpoint must be configured so that
Asterisk does not apply external-address rewriting to Tailscale peers. The
Tailscale CGNAT range is `100.64.0.0/10`:

```ini
[tg-bridge]
type=endpoint
...
local_net=100.64.0.0/10
external_media_addr=100.x.x.x  # GSM node's Tailscale IP
nat_option=rtp
```

- `local_net` — tells Asterisk that peers on this network should be addressed
  directly (no NAT traversal)
- `external_media_addr` — the IP the bridge should send RTP to (GSM node's
  Tailscale IP). In single-node mode this is not needed.
- `nat_option=rtp` — use RTP for media address detection on the tailnet

### Link Drop Handling

If the Tailscale link drops mid-call, both legs must terminate cleanly:

1. The call registry exposes `get_bridged_calls()` for health monitoring
2. On link failure, `terminate_bridged_calls(reason="link_drop")` hangs up
   all bridged calls
3. AMI `hangup_channel()` is called for each active channel (GSM + bridge)
4. The user is notified via Telegram that the call was terminated

**Verification (MANUAL_VERIFY):** Run `tailscale down` on one node during an
active call. Both sides should see the call end within seconds.
`core show channels` should be empty after cleanup.

### Architecture — Distributed

```
Telegram User ──MTProto+WebRTC──► sip-tg-bridge (Telegram node, 100.a.b.c)
                                        │
                                   SIP 5062
                                        │
                                 ┌───────┴───────┐
                                 │    Tailscale   │  ← encrypted (WireGuard)
                                 │  100.64.0.0/10 │  ← plain RTP on top
                                 └───────┬───────┘
                                        │
                                   SIP + RTP (plain)
                                        │
                              Asterisk (GSM node, 100.x.y.z)
                                        │
                                   chan_dongle
                                        │
                                   GSM Network
```
