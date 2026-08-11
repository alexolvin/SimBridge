# Voice Bridge Architecture (Stage 04)

## Decision: Plain RTP over Tailscale, no SRTP

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

## Bridge Selection

Evaluated in order:

1. **foobar26/tg2sip** (PRIMARY) — maintained fork of Infactum/tg2sip,
   rebuilt on ntgcalls (WebRTC-based). Bridges native Telegram P2P voice
   calls to SIP. Distributed as Docker.

   Deployment alongside Asterisk: tg2sip listens on UDP 5062 while
   Asterisk keeps 5060. The PJSIP endpoint is defined with
   `disallow=all`, `allow=ulaw,alaw`, `direct_media=no`.

2. **blitss/sip-tg-bridge** (FALLBACK) — Go, LiveKit-derived SIP/RTP
   pipeline. Self-described as POC/WIP.

3. **Direct ntgcalls integration** (LAST RESORT) — most control, most work.

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
Telegram User ──MTProto+WebRTC──► tg2sip (Telegram node)
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