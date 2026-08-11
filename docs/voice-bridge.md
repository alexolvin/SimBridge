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