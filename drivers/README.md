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

### Apply, build, install

```sh
# on the node, in the build tree
cd /home/user/asterisk-chan-dongle
git am /path/to/SimBridge/drivers/asterisk-chan-dongle/0001-*.patch
make          # needs: gcc, make, asterisk-devel (18.x)

sudo cp /usr/lib64/asterisk/modules/chan_dongle.so \
        /usr/lib64/asterisk/modules/chan_dongle.so.bak-$(date +%Y%m%d-%H%M%S)
sudo cp chan_dongle.so /usr/lib64/asterisk/modules/
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
        /usr/lib64/asterisk/modules/chan_dongle.so
sudo systemctl restart asterisk
```

### Known open questions (as of 2026-08-26)

- Voice path on EC25-EU (serial PCM via `AT+QPCMV`) is not yet confirmed
  end-to-end with audio. The URC routing + crash fixes are independent of
  it; a live instrumented call is pending.
- `AT+QURCCFG` NVRAM persistence across a module reboot (`AT+CFUN=0/1`)
  is unverified — no longer functionally critical, since the driver re-issues
  the setting on every device init (asterisk restart / USB re-scan).
