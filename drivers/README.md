# Asterisk channel drivers

SimBridge runs on **chan_dongle** (wiringSoft / wdoekes fork). Production does
not use the off-the-shelf packages (wiringSoft / PPA) — it uses a locally
maintained build with SimBridge patches. This directory keeps the provenance
so the production module can be rebuilt from scratch.

## asterisk-chan-dongle

- **Upstream:** <https://github.com/wdoekes/asterisk-chan-dongle>
- **Base:** `master` @ `31eb619` (Feb 2026, includes PR #183)
- **Local fork commits** (in the build tree history, not upstream):
  - `smsdb` — per-destination SMS delivery-report tracking (sqlite3),
    used by the agent's delivery-report correlation
  - Quectel EC25 voice support — `has_voice_quectel`, `AT+QPCMV`
    (the upstream `AT^CVOICE`/`AT^DDSETEX` path is Huawei-only)
- **Build tree on the node:** `/home/user/asterisk-chan-dongle` (3p14-aaa).
  This is the source of truth for the full fork; the patches below apply on
  top of it.

### Patches (in order)

| Patch | What it fixes |
|---|---|
| `asterisk-chan-dongle/0001-fix-unsolicited-no-carrier-crash-quectel-urcport.patch` | (1) **SIGSEGV crash**: `at_response_busy()` dereferenced the AT queue head task without a NULL check — an unsolicited `NO CARRIER` arriving while the queue is empty (network releases an already-answered call) crashed Asterisk (incident 2026-08-26 04:58 MSK, coredump-confirmed). (2) `NO CARRIER` with an empty queue now resyncs the call table via `AT+CLCC`, so a still-open call is torn down cleanly (hangup-exten, recording preserved) instead of crashing. (3) `+CMGS` delivery report with no pending command is ignored instead of crashing. (4) `AT+QURCCFG="urcport","usbat"` is issued automatically after `AT+QPCMV?` succeeds (Quectel only) — the EC25 factory default routes call URCs (RING, +QIND, NO CARRIER) to the modem/PPP port where the driver cannot see them, so incoming calls never reached the PBX. |
| `asterisk-chan-dongle/0002-ec25-call-teardown-no-cend-stale-cpvt.patch` | Call-lifecycle defects on modules that never send `+CEND` (Quectel EC25): (1) **zombie call + stuck device** — an unsolicited `NO CARRIER` with an empty command queue only requested a CLCC resync, but the driver's dead-cpvt cleanup loop in `at_response_clcc()` is unreachable (the parse loop always returns before reaching it) and a bare-OK (empty) CLCC dispatches no CLCC handler at all — so the channel stayed alive until `Wait()` expired (~72 s later) and the device stayed stuck in `Ring` (Calls/Channels: 1). `NO CARRIER` now releases the tracked call(s) directly via `change_channel_state(RELEASED)` (canonical teardown: hangup-exten, ring flag, cpvt free) and still resyncs via CLCC. (2) **swallowed next call** — the network reuses the call index for the next incoming call; the stale cpvt (no channel, still INCOMING/WAITING) was found by `pvt_find_cpvt()` and the new call was silently dropped. The stale entry is now freed before the PBX is started for the new call. (3) `CEND` for an untracked call index logs a WARNING (was silent). Live-verified 2026-08-26: hangup teardown in <1 s, device back to Free, next call gets through. |
| `asterisk-chan-dongle/0003-uac-voice-alsa-quectel-ec25.patch` | UAC (USB Audio Class) voice transport — the EC25 carries voice over a virtual USB sound card, not the serial audio tty (live call 2026-08-26: serial port dead from frame 0). New device setting `alsadev=<ALSA device>` (e.g. `hw:CARD=EC25EUX,DEV=0`): opens ALSA capture + playback at 8 kHz s16 mono, the capture poll fd feeds the channel select loop, frames are read/written directly against the streams with EPIPE recovery and per-call stream start/drop. In UAC mode `AT+QPCMV=1,2` (USB audio) is armed before `ATD`/`ATA` and the post-dial `AT+QPCMV=1,0` is skipped; the monitor skips the tty status check for the ALSA fd. Requires a one-time NVRAM `AT+QCFG="USBCFG"` with UAC=1, `alsadev` in the device section of dongle.conf, the asterisk user in the `audio` group, and `alsa-lib-devel` at build time. **Live call verification pending** — production stays on 0002 (serial) until then. |

### Apply, build, install

```sh
# on the node, in the build tree
cd /home/user/asterisk-chan-dongle
git am /path/to/SimBridge/drivers/asterisk-chan-dongle/0001-*.patch
git am /path/to/SimBridge/drivers/asterisk-chan-dongle/0002-*.patch
git am /path/to/SimBridge/drivers/asterisk-chan-dongle/0003-*.patch
make          # needs: gcc, make, asterisk-devel (18.x), alsa-lib-devel (0003)

sudo cp /usr/lib64/asterisk/modules/chan_dongle.so \
        /usr/lib64/asterisk/modules/chan_dongle.so.bak-$(date +%Y%m%d-%H%M%S)
# Replace via temp name + atomic rename (new inode). Never cp over the
# live path in place: the running asterisk has the old file mmap'd and
# an in-place overwrite corrupts its pages (it crashed the old process
# on 2026-08-26 06:21 during a deploy).
sudo cp chan_dongle.so /usr/lib64/asterisk/modules/chan_dongle.so.new
sudo mv /usr/lib64/asterisk/modules/chan_dongle.so.new \
        /usr/lib64/asterisk/modules/chan_dongle.so
sudo systemctl restart asterisk
```

### Verify (Rule 2 — every "works" needs an artifact)

```sh
asterisk -rx 'dongle show devices'        # gsm: Free, camped, SIM ready
journalctl -u asterisk --since '2 min ago' # no ERROR from chan_dongle
```

Functional canary: an incoming call produces `+CRING`/RING URCs on the AT
port → Telegram notification (this only works once URCs are routed to the
AT port, i.e. patch 0001 is in).

### Rollback

```sh
sudo cp /usr/lib64/asterisk/modules/chan_dongle.so.bak-<timestamp> \
        /usr/lib64/asterisk/modules/chan_dongle.so.new
sudo mv /usr/lib64/asterisk/modules/chan_dongle.so.new \
        /usr/lib64/asterisk/modules/chan_dongle.so
sudo systemctl restart asterisk
```

### Known open questions (as of 2026-08-26)

- Voice path on EC25-EU: the serial audio port is dead from frame 0 (live
  instrumented call, 2026-08-26); per Quectel docs the module's voice is UAC
  (virtual USB sound card). UAC support is implemented in **patch 0003**
  (ALSA capture/playback backend, `alsadev` setting, `AT+QPCMV=1,2` arming);
  the one-time `AT+QCFG="USBCFG"` UAC=1 was persisted to NVRAM on 2026-08-26.
  **Live call verification pending** — production stays on 0002 (serial) until
  a real call round-trips audio both ways.
- `AT+QURCCFG` NVRAM persistence across a module reboot: **verified
  2026-08-26** — `"urcport","usbat"` survived a full `AT+CFUN=0/1` cold
  reset (read back after re-registration). The driver still re-issues the
  setting on every device init (asterisk restart / USB re-scan) as
  insurance for a fresh module or NVRAM wipe.
