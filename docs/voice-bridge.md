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
- **Go bindings**: Available via CGO. Example in `./examples/go/`.
- **Assessment**: Would give maximum control but requires building the SIP layer ourselves. Only pursue if sip-tg-bridge proves insufficient.

### Selection: blitss/sip-tg-bridge (FALLBACK → PRIMARY by default)

**Reasoning**: The primary candidate (foobar26/tg2sip) is unavailable. sip-tg-bridge uses ntgcalls (not libtgvoip), is built on LiveKit SIP (battle-tested), and is explicitly designed as a tg2sip substitute. The POC status is a risk, but it's the only ntgcalls-based bridge with SIP integration that is publicly available.

**Contingency**: If sip-tg-bridge fails in practice (build issues, call reliability), fall back to direct ntgcalls integration using Go bindings + custom pjsip wrapper.

**Honesty note on the control API**: sip-tg-bridge is a POC whose HTTP control surface does not (as of the research date) match the contract below (§ Bridge Control API). The SimBridge-side client (`userbot/bridge_control.py`) is written against the **required** contract; the POC must be adapted or forked to match it. That adaptation is **MANUAL_VERIFY** — it has not been built or exercised yet (see the MANUAL_VERIFY list at the end of this document).

**MANUAL_VERIFY**: Build sip-tg-bridge from source and place a real Telegram voice call. This cannot be verified in this session without the build environment.

---

## Transport Decision: Plain RTP over Tailscale, no SRTP

Tailscale (WireGuard) provides point-to-point traffic encryption.
Every packet between the two nodes is encrypted at the network layer.
The device's private key never leaves the device — Tailscale cannot
decrypt the traffic either.

Adding SRTP would mean:
- A second key infrastructure (DTLS-SRTP requires certificate exchange)
- Per-packet CPU cost for encrypting already-encrypted traffic
- Certificate management overhead

This is a **duplicated mechanism** — refused under Rule 1.

Plain RTP over the tailnet is the correct choice.

**When SRTP becomes necessary**: if the transport ever changes to
something untrusted (public internet without WireGuard, shared hosting,
etc.), enable `voice.srtp: true` in the config. The generator emits the
`encryption` settings for that mode; the bridge must support it as well.

## Where the Bridge Runs

On the **Telegram node**, next to the userbot. Telegram traffic
(MTProto + WebRTC voice) terminates there. Only SIP + RTP crosses
the tailnet to the GSM node.

This preserves the geographic separation: Telegram connection from
one location, SIM card physically elsewhere.

---

## Call Control Design: the Bridge-UAC Hybrid

The bridge plays a **different SIP role in each direction**. This is
the core Stage 04 design decision.

### The ordering constraint

- **Incoming** (GSM caller → TG user): the TG user must be rung, and the
  GSM leg may be answered **only after the user accepts**. The dialplan
  can express this natively: `Dial()` to a SIP endpoint blocks, rings
  the endpoint, and auto-answers when it returns 200.
- **Outgoing** (TG user → GSM target): the TG user must be rung first,
  and the real GSM number may be dialed **only after the user accepts**.
  Only the bridge knows the moment of acceptance (it is the party
  carrying the Telegram call). A dialplan on the GSM node cannot express
  "wait for an external event that happens on another machine" — so the
  GSM node must not initiate this call. The **bridge initiates it**: when
  the TG user accepts, the bridge sends the SIP INVITE to the GSM node's
  Asterisk.

Hence: **incoming = bridge is UAS (Asterisk dials it); outgoing = bridge
is UAC (the bridge dials Asterisk).**

### Alternative evaluated and rejected: bridge as UAS in both directions

The natural "one role" design has Asterisk initiate the SIP leg in both
directions. Incoming: correct (that is the kept half). Outgoing: wrong —
Asterisk would have to `Dial(SIP/bridge)` and *then* make the bridge ring
the TG user, which either (a) dials the real target before the TG user
has accepted (a stranger's phone rings in the world for a call that may
never be wanted), or (b) holds the channel in a `WaitForever`-style
block for the out-of-band Telegram ring — which defeats the `Dial`
timeout, the `DIALSTATUS` contract and the hangup handler, and gives the
timeout driver nothing to reason about. The hybrid avoids all of this:
each direction uses the party that already knows its own timeout.

### Incoming (bridge = UAS)

```
GSM caller ──► chan_dongle [incoming-mobile/s]
    │  1. AGI incoming → agent /v1/call/incoming (register, TELEGRAM_RINGING)
    │  2. Dial(SIP/${BRIDGE_ENDPOINT},${RING_WAIT_SECONDS})
    │       └─► PJSIP endpoint → bridge → Telegram call to the user
    │           GSM channel stays UNANSWERED — the GSM caller hears
    │           real carrier ringback, not a local tone
    │  3. AGI complete ${DIALSTATUS} → agent /v1/call/{id}/complete
    │  4. GotoIf($["${DIALSTATUS}" = "NOANSWER"]?voicemail)
    └─► Hangup()
```

| Dial returns | Meaning | Dialplan outcome |
|---|---|---|
| `ANSWERED` | TG user accepted — media bridged, call runs | `Hangup()`; the call ends later via the h-exten (`ENDED`) |
| `NOANSWER` | Telegram ring window expired | **voicemail** (named branch below) |
| `BUSY` | bridge answered 486/403 — the user rejected | `Hangup()` (rejected, no voicemail) |
| `CANCEL` | GSM caller hung up while ringing | `Hangup()` |
| anything else (`CHANUNAVAIL`, `CONGESTION`, …) | the bridge leg itself failed, e.g. bridge down | `Hangup()` — **no voicemail**: the voicemail branch is a *Telegram-timeout* fallback, not a bridge-failure fallback (a dead bridge has no Telegram leg to blame, and the honest outcome is "call failed") |

The voicemail branch is therefore entered **only on `NOANSWER`**.

### Outgoing (bridge = UAC)

```
TG user sends a bare phone number (e.g. "+79261234555")
    │  userbot handle_bare_number:
    │    extract_call_request() → ACL out_call → normalize → blacklist
    │    → agent /v1/call/outgoing (rate-limit, ACL re-check, blacklist,
    │      atomic modem reservation, TELEGRAM_CALLING)
    │    → bridge control API (loopback) POST /call — the bridge starts
    │      the Telegram call to the user
    │
    │  TG user accepts →
    ▼
bridge INVITEs sip:<target>@<GSM node>:5060
    │  (From-user = the tg-bridge endpoint; Request-URI user = target)
    ▼
Asterisk [tg-bridge] context, EXTEN = target
    │  1. Answer()          ← the SIP leg (and the TG call) is live
    │  2. AGI outgoing-accepted → agent /v1/call/outgoing/accepted
    │       └─ 200 → SET VARIABLE CALL_ID <id>
    │          404 → CALL_ID stays empty (nocal gate below)
    │  3. GotoIf($["${CALL_ID}" = ""]?nocal)     ← the nocal gate
    │  4. Dial(Dongle/${MODEM_ID}/${EXTEN},${OUTBOUND_GSM_RING_SECONDS})
    │       └─ the TG user hears the target's REAL ringback
    │          (the two-party bridge passes in-band ringback)
    │  5. AGI complete ${DIALSTATUS} → agent /v1/call/{id}/complete
    │       └─ per-outcome userbot notification (answered / no_answer /
    │          busy / failed — separate localized messages)
    └─► Hangup()
```

**The nocal gate** (step 3): if the Telegram ring already timed out when
the user finally accepted, `/call/outgoing/accepted` returns 404 (the
call was closed by the timeout driver), `CALL_ID` is never set, and the
dialplan hangs up **without dialing the target**. A late accept must not
ring a real phone for a call that no longer exists.

**Modem reservation**: `/v1/call/outgoing` takes the reservation
atomically in the agent (single call per node while the pool is a
single member); if the bridge cannot start the Telegram call the
userbot rejects the agent-side call immediately (best-effort), and
`/v1/call/check-timeouts` is the backstop that reaps any call left in
`TELEGRAM_CALLING` past `voice.outbound_answer_timeout` (default 30 s).

### Bridge Control API (loopback contract)

The userbot and the bridge run side by side on the Telegram node, so the
control API is **loopback-only** (`127.0.0.1:<voice.bridge_control_port>`,
default 5063) and is never exposed on the Tailscale interface. Loopback
binding is an architectural invariant (the bridge holds the Telegram
session), not a configurable; the port is configurable
(`voice.bridge_control_port`).

```
POST /call    {"user_id": int, "target": "<E.164>",
               "gsm_host": "<SIP host of the GSM node>",
               "gsm_port": 5060}
    → 2xx: the bridge starts a Telegram call to user_id; when the user
      accepts, the bridge INVITEs sip:<target>@<gsm_host>:<gsm_port>.
    → non-2xx / unreachable: no Telegram call was started.
POST /cancel  {"user_id": int}
    → 2xx: the in-progress Telegram ring/call for user_id is cancelled.
```

- Auth: Bearer token from the environment variable named by
  `userbot_http.secret_env` (SIMBRIDGE_HTTP_SECRET) — the same secret
  domain as the agent → userbot event channel.
- `gsm_host` is derived from `agent.listen` (the userbot already reaches
  the GSM node's agent there) — no separate config key (Rule 1).

### No-orphan-legs proof (Asterisk source, verified 2026-08-15)

Requirement: a leg on one side must never survive independently. Both
directions rely on the Dial app's hangup propagation:

- **Incoming**: if the TG user hangs up, the SIP channel dies. The Dial
  app tears down the rest via its hanguptree — `apps/app_dial.c`
  `dial_check_hangup()` (line ~834) and the peer-hangup handling (line
  ~3322) mark the other party for hangup when a party goes. The GSM
  channel follows.
- **Outgoing**: the SIP channel executes the `[tg-bridge]` dialplan, so
  when the TG user hangs up the channel dies and the Dial app
  cancels/hangs up the Dongle leg — `apps/app_dial.c` hanguptree and
  `main/dial.c` `ast_dial_destroy()` (lines ~1069/~1091) destroy the
  pending/active dial. There is no orphan GSM call left ringing at the
  target.
- **Either direction, either party**: AMI `HangupChannel` of ONE leg
  cascades the same way (the channel's hangup triggers the hanguptree).
  The agent's `/complete` and `/check-timeouts` handlers therefore hang
  up legs via AMI and do not need to chase the other side.

The timeout driver (`simbridge-timeouts` systemd timer, 5 s period,
`scripts/call-timeout-check.py` → `POST /v1/call/check-timeouts`) is the
enforcement for the **out-of-band** Telegram ring (outgoing `TELEGRAM_CALLING`
window — no dialplan `Dial` exists to time it out) and a backstop for the
incoming ring (lost AGI event) and `limits.max_call_seconds` (bridged
calls past the cap are hung up via AMI per leg).

### Rejected design note: `WaitForever`

An early variant of the outgoing flow held the dialplan channel in a
`WaitForever`-style block while the Telegram user was being called.
Rejected: it defeats `Dial` timeouts and the hangup-handler contract,
and gives the timeout driver no state to observe. The shipped design
uses `Hangup()` + the nocal gate instead.

---

## PJSIP Endpoint (generated, not hand-edited)

The `tg-bridge` PJSIP endpoint is **generated** by
`scripts/generate_asterisk_config.py -p` from `simbridge.yaml` (the
bridge password comes from `SIMBRIDGE_BRIDGE_SECRET` in the process
environment). There is no `pjsip.conf.example` to edit by hand (Rule 1:
one mechanism, the generator). Single-node output:

```ini
[global]
type=global
user_agent=SimBridge

[transport-udp]
type=transport
protocol=udp
bind=127.0.0.1
local_net=100.64.0.0/10

[tg-bridge]
type=endpoint
transport=transport-udp
context=tg-bridge
disallow=all
allow=ulaw,alaw
dtmf_mode=rfc4733
; S04.2: Asterisk MUST relay the media — the bridge is the far party,
; direct (pass-through) media would expose the bridge's address to the
; Dongle and break on NAT.
direct_media=no
rtp_timeout=60
rtp_timeout_hold=30
ice_support=no
auth=tg-bridge-auth
outbound_auth=tg-bridge-auth
aors=tg-bridge-aor
; S04.5: default identify_by is username,ip. The bridge (UAC)
; authenticates as tg-bridge but its From user is not this AOR name,
; so it is only identifiable via the Authorization header. Without
; auth_username the request is not identified to this endpoint and is
; authed against the artificial endpoint instead: 401 despite correct
; credentials. Root-caused 2026-08-19 via core set debug 3
; (res_pjsip_authenticator_digest.c).
identify_by=username,ip,auth_username

[tg-bridge-auth]
type=auth
auth_type=userpass
username=tg-bridge
password=<SIMBRIDGE_BRIDGE_SECRET>

[tg-bridge-aor]
type=aor
max_contacts=1
contact=sip:127.0.0.1:5062
```

> Every option above is audited against the Asterisk 18 sorcery field
> registrations (res/res_pjsip/*). A section carrying any unknown key is
> dropped as a whole (live incident 2026-08-17/18, 3p14-aaa: the
> pre-16 spelling `dtmf_mode=rfc2833` plus legacy chan_sip keys
> `rtptimeout`/`rtpholdtimeout`/`nat_option` and a chan_sip `qualify`
> on the aor left the node with a running chan_pjsip and zero
> registered objects).

- Inbound auth is **mandatory**: without it, anyone who can reach the
  transport could dial arbitrary GSM numbers through the modem
  (outgoing) or inject fake incoming calls.
- `context=tg-bridge` routes the bridge's INVITEs to the `[tg-bridge]`
  dialplan context (outgoing GSM leg, above).
- `identify_by=username,ip,auth_username` is **required**, not
  cosmetic: with the default `username,ip` the bridge's INVITE (From
  user ≠ AOR name, authenticated as `tg-bridge`) is not identified to
  this endpoint, so PJSIP falls back to the *artificial* endpoint for
  auth — correct credentials then yield 401 anyway. Live root cause
  2026-08-19 (3p14-aaa), found via `core set debug 3` on
  res_pjsip_authenticator_digest. Note the related NOTICE
  "No matching endpoint found for ... using_auth_username=0" is
  **by design** for unauthenticated INVITEs: `using_auth_username` is
  only set when a global `endpoint_identifier_order` is configured,
  which SimBridge does not (identification must stay explicit).

**Distributed diff** (generator, when `voice.bridge_host` is not
loopback and the node has a Tailscale IP): `bind=<this node's Tailscale
IP>` (a wildcard SIP listener is a finding, not a feature — S06.1),
`external_media_address=<this node's Tailscale IP>` added to the
TRANSPORT, and `contact=sip:<bridge_host>:5062` in the AOR. See
§ Distributed Mode.

## Media Flow

```
Incoming:
GSM Network ──► chan_dongle ──► Asterisk (GSM node)
                                      │  SIP + RTP (plain, over tailnet)
                                      ▼
                        sip-tg-bridge (Telegram node, UDP 5062)
                                      │  WebRTC
                                      ▼
                                Telegram user

Outgoing:
Telegram user ◄──WebRTC── sip-tg-bridge
                              │  SIP INVITE (on accept) + RTP
                              ▼
Asterisk (GSM node) ──► chan_dongle ──► GSM Network (target)
```

---

## Voicemail Fallback (S03.4 — preserved)

The voicemail branch is a **named, same-context branch** `voicemail`
inside `[incoming-mobile]`, entered with `GotoIf($["${DIALSTATUS}" =
"NOANSWER"]?voicemail)` after the Stage 04 `Dial`. It is the single,
**unchanged fallback target** of the Stage 04 state machine (Rule 4:
the Stage 03 voicemail behavior is preserved byte-for-byte; Stage 04
only adds the `Dial` in front of it).

Why a same-context named exten and not a separate context: the h-exten
resolves in the channel's *current* context, so a voicemail path in a
different context would need its own h-exten — a second forwarding
mechanism (Rule 1). The named branch keeps everything in
`[incoming-mobile]`.

```
chan_dongle (incoming-mobile/s)
    │
    ├── AGI(tg-blacklist-agi.py)          → BL_BLOCKED → Busy(5)
    ├── AGI(tg-sms-agi.py, ring)          → Telegram in_call audience
    ├── AGI(notify-agent-agi.py,incoming) → agent registers the call
    ├── Dial(SIP/${BRIDGE_ENDPOINT},${RING_WAIT_SECONDS})
    │       accept → auto-answer + bridge; reject → BUSY; timeout → NOANSWER
    ├── AGI(notify-agent-agi.py,complete) → agent records the outcome
    └── GotoIf($["${DIALSTATUS}" = "NOANSWER"]?voicemail)
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
                        ├── AGI(tg-voice-agi.py) → core.voicemail_forward
                        │   → userbot /events/voicemail
                        └── AGI(notify-agent-agi.py,complete,ENDED)
                            (dead mode; 404 = already terminal = no-op)
```

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

**No audio to disk in the voice path**: the bridge is a live media
relay; no side of a bridged call is recorded (unlike voicemail, where
recording is the product).

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

That is the only code-relevant difference. All other changes are made by
the PJSIP generator (bind, external_media_address, AOR contact — § PJSIP
Endpoint). There is no second mechanism: the same generator, the same
endpoint, parameterized.

### Addressing: MagicDNS FQDN or Raw Tailscale IP

Use either the full MagicDNS FQDN (`my-node.tail-<netid>.ts.net`) or the raw
Tailscale IP (`100.x.x.x`). **Never use short hostnames** — they may not
resolve reliably across all nodes in the tailnet.

### PJSIP local_net and NAT Settings

For the distributed mode, the generated PJSIP transport declares the
Tailscale CGNAT range `100.64.0.0/10` as local so that Asterisk does not
apply external-address rewriting to Tailscale peers. NAT traversal
(comedia) is built into Asterisk 18 pjsip — there is no nat option to
configure (the legacy `nat_option` key no longer exists). `local_net`
and `external_media_address` are TRANSPORT fields (sorcery registration
in res/res_pjsip/config_transport.c), not endpoint fields:

```ini
[transport-udp]
type=transport
protocol=udp
bind=100.x.x.x  # this node's Tailscale IP (S06.1: no wildcard bind)
local_net=100.64.0.0/10
external_media_address=100.x.x.x  # this node's Tailscale IP
```

- `local_net` — tells Asterisk that peers on this network should be
  addressed directly (no NAT traversal)
- `external_media_address` — the IP to advertise for this side's RTP
  (this node's Tailscale IP). Not needed in single-node mode.

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

---

## Known trade-offs

- **8-digit numeric false positive**: `extract_call_request()` treats any
  8+ digit string as a call request, so a PIN-like code ("12345678")
  matches and rings that number. Accepted trade-off: the alternative
  (country-code whitelisting) would be a second, config-dependent
  classification mechanism for the same input; the false positive is
  harmless (one missed ring) while a missed real number would be a
  dropped call. Pinned by a unit test so a future change is deliberate.

## Operational Findings — 3p14-aaa, 2026-08-19/20

Commissioning findings from the live GSM node. Each is pinned by the
generator / installer / tests where a code artifact exists.

### SDP/RTP modules are not auto-loaded (reboot hazard)

`res_pjsip_sdp_rtp.so` and `res_rtp_asterisk.so` (the "asterisk" RTP
engine) are **not a `<depend>` of any module** — verified in the
18.26.4 source and in the strings of the installed EPEL `.so` files.
Under `autoload=no` they load only if listed in `modules.conf`. On
2026-08-19 they were loaded at runtime via the `module load` CLI and
lived only in process memory: the voice bridge worked until the next
restart, which would have dropped both modules and broken all PJSIP
media silently (488 on INVITEs, `No RTP engine was found`). Now
persisted in `modules.conf` (live) and `AST_MODULES_LOAD` (installer);
regression test in `tests/test_install_noninteractive.py`.
**Lesson: a runtime `module load` is not a configuration — anything
that only exists in process memory is lost at the next (re)start.**

### Asterisk log `[NUM]` is the thread ID

The `[NUM]` in log lines is the **thread** ID, not the main PID — on a
host that churns ~30 PIDs/s (pid_max 4194304), PJSIP thread-pool TIDs
differ from the main PID by millions, and reading them as separate
asterisk instances is a false lead. Verify process identity with
`ps -eo pid,lstart,cmd`, not with log numbers.

### No-user E2E SIP probe — ALL PASS (2026-08-20)

`scripts/e2e_sip_probe.py`, run from the TG node against the live GSM
node — a synthetic authenticated SIP client (correct `tg-bridge`
digest) plus a raw RTP socket; no real Telegram account involved:

- **Phase A** (media, exten 778): 401 challenge → 200 OK → **250 RTP
  packets received** (5 s of silence, ulaw) → BYE from Asterisk.
- **Phase B** (nocal, exten 777 — matches `_X.` → the outgoing
  branch's `outgoing-accepted` AGI finds no registered call → `nocal`
  → Hangup without Dial): 401 → 200 OK → BYE, zero RTP (by design —
  the nocal branch answers but plays nothing).

This is a real-device run (Rule 3) for the SIP + media path across the
tailnet. TS04-2 (a real Telegram voice call, both directions) remains
open.

Probe bugs found while making it pass (all fixed in the script): the
RTP counter `data[0] & 0x60 == 0x80` is always False — the correct
u-law check is `data[0] & 0xC0 == 0x80` (the media path was fine all
along; `rtp_rx=0` was the probe's own bug); and Phase B read the
extension after Phase A's reset had overwritten it (now a stable
`b_exten` copy).

## MANUAL_VERIFY items (Stage 04)

Live-device evidence (Rule 3) — to be closed in the test & fix pass with
real modem + real Telegram account:

- **TS04-1**: real build of sip-tg-bridge + adaptation of its control API
  to the loopback contract above (POC has no matching API).
- **TS04-2**: real Telegram voice call end-to-end, both directions
  (incoming accept / reject / timeout→voicemail; outgoing accepted /
  no-answer / busy / cancelled). SIP + media leg verified without a
  user via the E2E probe (see Operational Findings above); the
  Telegram-account leg remains.
- **TS04-3**: link-drop run (`tailscale down` mid-call) — clean
  termination, no orphan channels (`core show channels` empty).
- **TS04-4**: distributed two-node run (GSM node + Telegram node,
  `voice.bridge_host` = Tailscale IP) — voice across the tailnet.
- **TS04-5**: confirm whether foobar26/tg2sip (or a renamed equivalent)
  exists — the S04.1 research found it missing (404).
