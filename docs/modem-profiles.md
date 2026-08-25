# Modem profiles — per-modem config with one-command swap

SimBridge runs on a USB GSM modem. Everything that is **modem-specific** lives
in exactly four artifacts on the node:

| # | Artifact | What is modem-specific |
|---|----------|------------------------|
| 1 | `/etc/udev/rules.d/92-dongle.rules` | USB PID + interface-number → `dongle_ifN` symlinks, `asterisk:dialout` ownership |
| 2 | `/etc/asterisk/dongle.conf` | channel name, `audio =`, `data =` devices |
| 3 | `/etc/simbridge/simbridge.yaml` | `asterisk.dongle`, `sim.modem_model` (two keys) |
| 4 | `/etc/asterisk/asterisk-globals.conf` | `MODEM_ID` (generated from #3 by the existing `generate_asterisk_config.py`) |

Everything else — secrets (`/etc/simbridge/env`), ACL, blacklist, contacts,
Tailscale addresses, the dialplan, PJSIP, `ring_wait_seconds` — is
**not** modem-specific and is never touched by a profile switch.

A **profile** is one YAML file that holds the whole modem-specific state, so
swapping the physical modem becomes one command that re-renders all four
artifacts:

```
/etc/simbridge/modem-profiles/<name>.yaml   — one file per modem (no secrets)
/etc/simbridge/active_modem                 — one line: active profile name
```

`scripts/modem_profile.py` is the **only** mechanism that renders
`dongle.conf` and the dongle udev rule (Rule 1). `deploy/install.py` creates
the profile on fresh installs via the `init` subcommand; it never writes
those files itself.

## Profile format

```yaml
name: huawei-e161
modem_model: "Huawei Technologies Co., Ltd. E161/E169/E620/E800 HSDPA Modem"
vid_pid: "12d1:1001"
dongle_name: gsm                 # chan_dongle channel name (dialplan uses it)
audio_device: /dev/dongle_if1
data_device: /dev/dongle_if2
sim_phone: "+79000000000"        # informational: where the SIM currently is
udev:                            # USB interface number -> stable symlink
  - {iface: "00", symlink: dongle_if0}
  - {iface: "01", symlink: dongle_if1}
  - {iface: "02", symlink: dongle_if2}
verified: true                   # true ONLY after a live end-to-end test
notes: "captured from live state"
```

Reference examples: `config/modem-profiles.example/` in the repository.

## Commands

All commands (run as root on the node):

```bash
MP="python3 /opt/simbridge/scripts/modem_profile.py"

$MP list                 # all profiles, [ACTIVE] marker, verified state
$MP show [name]          # print one profile (default: active)
$MP status               # active profile vs plugged-in USB device,
                         # dongle registration, agent state

$MP new <name> [opts]    # create an UNVERIFIED template
$MP capture <name>       # create a VERIFIED profile from the live state
                         # (run while the current modem still works)
$MP probe [--commit <name> --data DEV --audio DEV]
                         # inspect the plugged-in modem: ports, AT+CGMM,
                         # AT+QCFG="USBCFG"; --commit writes the findings
$MP apply <name>         # re-render udev + dongle.conf (no restarts)
$MP use <name> [--force] # FULL SWITCH (see below)
```

`init` is for the installer and is not used by hand.

### What `use` does (in order)

1. Refuses an unverified profile without `--force` (Rule 2).
2. No-op check: if the profile is already active and all artifacts are
   byte-identical, it prints "nothing to do" and exits — **no restarts**.
3. Backs up all four artifacts (timestamped `.bak-<UTC>`), then re-renders
   the udev rule and `dongle.conf` from the profile.
4. Surgically updates exactly two keys of `simbridge.yaml`
   (`asterisk.dongle`, `sim.modem_model`) — every other byte preserved.
5. Writes the `active_modem` pointer.
6. Regenerates `asterisk-globals.conf` with the existing
   `generate_asterisk_config.py` (no second mechanism, Rule 1).
7. Restarts `asterisk`. Restarts `simbridge-agent` **only** if the channel
   name actually changed (the agent reads its config once at startup).
8. Verifies: `dongle show devices` must list the channel, the agent must be
   `active` (4 × 3 s retry while Asterisk comes up). On failure it exits 1
   and prints the exact rollback command and the backups it made.

The udev reload is scoped to the tty subsystem
(`udevadm trigger --subsystem-match=tty`) — a bare `udevadm trigger` would
re-fire every device on the box.

## Swap workflow — replacing the modem physically

Precondition: the profile for the *old* modem already exists (capture it
first, below), so rollback is always one command.

```bash
# 1. Old modem is still in and working. Plug in the NEW modem.
$MP status                              # see what is plugged in

# 2. Inspect the new modem (ports, AT port, USB mode).
$MP probe
#    Read the mapping. For a module where ttyUSB indices shifted, trust the
#    interface numbers, not the ttyUSB index.

# 3. Write the findings into the new profile (starts unverified).
$MP probe --commit <new-profile> --data /dev/ttyUSB2 --audio /dev/ttyUSB0

# 4. Switch. Unverified, so --force is required (an explicit acknowledgment).
$MP use <new-profile> --force

# 5. Live end-to-end check (SMS/call) — per policy only on explicit command.
#    On success, set `verified: true` in the profile.
```

**Rollback at any point**: put the old modem back in the USB port and run

```bash
$MP use <old-profile>
```

All four artifacts of the old modem are stored in its profile and are
re-created by the command; every previous state also exists as a
`.bak-<timestamp>` file next to the artifact.

### Capturing the current modem (do this BEFORE unplugging it)

```bash
$MP capture huawei-e161
```

Reads the live `dongle.conf`, `simbridge.yaml`, and udev metadata of
`/dev/dongle_if*`, writes a profile marked `verified: true` (it reflects a
working state), and suggests `use` to make it active. From then on the
on-disk profile is the source of truth, not the live files.

## Guarantees

- **Idempotency** — `use <active>` with in-sync artifacts changes nothing
  and restarts nothing.
- **Backups** — every `use`/`init` backs up all four artifacts before
  writing.
- **No secrets** — profiles contain no secrets; `/etc/simbridge/env` is
  never read or written by this tool (Rule 5).
- **Stable symlinks** — udev rules key on USB PID + interface number, which
  survive port reassignment and ttyUSB-index shifts.

## Voice path on Quectel EC2x modems

Per the Quectel EC2x QCFG manual (§9.2) and the "Voice Over USB and UAC"
application note, `AT+QCFG="USBCFG"` answers with seven port flags:

```
+QCFG: "usbcfg",<vid>,<pid>,<diag>,<nmea>,<at_port>,<modem>,<rmnet>,<adb>,<uac>
```

The factory default for the EC25-EU (`0x2C7C:0x0125`) is `1,1,1,1,1,0,0` —
the NMEA port is exposed and `uac=0`. On this layout there is no separate
serial *audio* port: the voice PCM stream rides the **NMEA port**
(`AT+QPCMV=<enable>,0` per the app note; option 0 = USB NMEA port).
Two conditions:

1. `AT+QPCMV` must be supported by the firmware (the chan_dongle build
   queries it at connect time and re-arms it on every dial/answer — the
   setting is not preserved across module reboots, so per-call arming is
   exactly what is needed);
2. the NMEA port must be free of GNSS output: if
   `AT+QGPSCFG="outport"` reports `"usbnmea"`, issue
   `AT+QGPSCFG="outport","none"` once (this one IS preserved).

If instead `uac=1` in the USBCFG answer, voice is a virtual USB sound card
(UAC) and the production `chan_dongle` build — serial PCM only — cannot use
it: SMS and call delivery would work, live voice would not. `probe` decodes
the USBCFG flags, prints the voice path, and warns when the NMEA port is
still carrying GNSS output — the decision is made from probe evidence, not
assumption (Rule 2).
