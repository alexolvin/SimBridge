
#!/usr/bin/env python3
"""SimBridge — Single-file Interactive Installer.

Zero dependencies.  Download one file, run it, done.

    curl -L https://raw.githubusercontent.com/alexolvin/SimBridge/main/deploy/install.py -o install.py
    sudo python3 install.py

The script clones the SimBridge repository, installs system and Python
dependencies, configures services, and guides you through first-time setup.

Uses ONLY the Python standard library.
"""

from __future__ import annotations

import os
import re
import sys
import grp
import pwd
import secrets
import subprocess
import shutil
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

REPO       = "https://github.com/alexolvin/SimBridge.git"
SSH_REPO   = "git@github.com:alexolvin/SimBridge.git"
BRANCH     = os.environ.get("SIMBRIDGE_BRANCH", "main")

# ── Version ─────────────────────────────────────────────────────────────────
def run_q(cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)

# 1. Try VERSION file from the source (deploy/ is inside the repo)
_SCRIPT_DIR = Path(__file__).resolve().parent
_VER_FILE   = _SCRIPT_DIR.parent / "VERSION"
if _VER_FILE.exists():
    __version__ = _VER_FILE.read_text().strip()
else:
    # 2. Fallback: read git describe from the repo at runtime
    _r = run_q(f"cd {_SCRIPT_DIR.parent} 2>/dev/null && git describe --tags --always 2>/dev/null")
    __version__ = _r.stdout.strip() if _r.returncode == 0 else "0.0.0+unknown"

def _read_ver(dir_: str) -> str:
    """Read VERSION from a project directory."""
    v = Path(dir_) / "VERSION"
    return v.read_text().strip() if v.exists() else "unknown"

def _ver_tuple(v: str) -> tuple:
    """Parse '0.6.0' into (0, 6, 0) for comparison."""
    parts = v.split("+")[0].split(".")  # strip metadata
    return tuple(int(p) for p in parts if p.isdigit())

def _short_modem(raw: str) -> str:
    """'Bus 001 ... ID 12d1:1001 Huawei ... E173 ...' -> short model name.

    lsusb lines contain: Bus XXX Device XXX: ID vendor:product <full name>
    We extract the words after the ID field and keep the first two
    (e.g. 'Huawei E173', 'Quectel EC20'). For manual entries the raw
    string is returned as-is.
    """
    m = re.search(r"ID [0-9a-fA-F]+:[0-9a-fA-F]+\s+(.+)", raw)
    if m:
        parts = m.group(1).split()
        return " ".join(parts[:2]) if len(parts) >= 2 else parts[0]
    return raw  # manual entry — display as-is

INSTALL_DIR   = "/opt/simbridge"
VENV_DIR      = "/opt/simbridge-venv"
CONF_DIR      = "/etc/simbridge"
DATA_DIR      = "/var/lib/simbridge"
LOG_DIR       = "/var/log/simbridge"
ENV_FILE      = f"{CONF_DIR}/env"
CONF_FILE     = f"{CONF_DIR}/simbridge.yaml"
ACL_FILE      = f"{CONF_DIR}/acl.conf"
BLACKLIST_FILE = f"{CONF_DIR}/blacklist.txt"
SVC_USER      = "simbridge"
HANDOFF_DIR   = Path(".handoff")

# ── Asterisk (GSM nodes) ────────────────────────────────────────────────────
AST_DIR        = "/etc/asterisk"
AST_GLOBALS    = f"{AST_DIR}/asterisk-globals.conf"   # generated
AST_EXTENSIONS = f"{AST_DIR}/extensions.conf"         # from the repo
AST_PROMPT     = "/var/lib/asterisk/sounds/custom/vm-prompt.ulaw"
# Drop-in giving the Asterisk process SimBridge's env — the AGI hooks
# inherit their secrets from it (Rule 5: no secrets in units or dialplan).
AST_DROPIN     = "/etc/systemd/system/asterisk.service.d/simbridge-env.conf"
# Asterisk's AGI application dir is a compile-time constant — detect it
AGI_BIN_DIRS   = ("/usr/lib64/asterisk/agi-bin", "/usr/lib/asterisk/agi-bin")
# AGI hook scripts linked into the AGI dir (called by extensions.conf)
AGI_SCRIPTS    = ("tg-sms-agi.py", "tg-voice-agi.py",
                  "tg-blacklist-agi.py", "notify-agent-agi.py")

# ══════════════════════════════════════════════════════════════════════════════
# Terminal I/O
# ══════════════════════════════════════════════════════════════════════════════

class C:
    _on = sys.stderr.isatty()
    R  = "\033[0;31m" if _on else ""
    Y  = "\033[0;33m" if _on else ""
    G  = "\033[0;32m" if _on else ""
    C  = "\033[0;36m" if _on else ""
    B  = "\033[1m"    if _on else ""
    _0 = "\033[0m"    if _on else ""

def _w(s: str = "") -> None:
    print(s, file=sys.stderr)

def info(label: str, value: str = "") -> None:
    _w(f"{C.C}  {label} {value}{C._0}")

def warn(label: str, value: str = "") -> None:
    _w(f"{C.Y}  {label} {value}{C._0}")

def ok(label: str, value: str = "") -> None:
    _w(f"{C.G}  {label} {value}{C._0}")

def fail(label: str, value: str = "") -> None:
    _w(f"{C.R}  {label} {value}{C._0}")

def heading(title: str) -> None:
    _w(f"\n{C.B}=== {title} ==={C._0}\n")

# ─── Prompts ────────────────────────────────────────────────────────────────

def ask(label: str, default: str = "", *, required: bool = False) -> str:
    while True:
        suffix = f" (default: {default})" if default else ""
        sys.stderr.write(f"{C.C}? {label}{suffix}: {C._0}")
        sys.stderr.flush()
        answer = input().strip() or default
        if required and not answer:
            warn(f"  {label} is required.")
            continue
        return answer

def ask_yn(label: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    sys.stderr.write(f"{C.C}? {label} {hint}: {C._0}")
    sys.stderr.flush()
    ch = input().strip().lower()
    if not ch:
        return default
    return ch in ("y", "yes")

def pick(label: str, options: List[str], default_idx: int = 0) -> str:
    _w(f"{C.C}  {label}{C._0}")
    for i, opt in enumerate(options, 1):
        _w(f"    {i}) {opt}")
    n = len(options)
    while True:
        hint = f" (default {default_idx + 1})" if 0 <= default_idx < n else ""
        sys.stderr.write(f"{C.C}  Choose [1-{n}]{hint}: {C._0}")
        sys.stderr.flush()
        ch = input().strip()
        if not ch and 0 <= default_idx < n:
            ch = str(default_idx + 1)
        try:
            idx = int(ch) - 1
            if 0 <= idx < n:
                return options[idx]
        except ValueError:
            pass
        warn("  Invalid choice.")

# ══════════════════════════════════════════════════════════════════════════════
# Shell helpers
# ══════════════════════════════════════════════════════════════════════════════

def run(cmd: str, **kw: Any) -> subprocess.CompletedProcess[None]:
    """Run a command, output streamed to the terminal."""
    _w(f"    $ {cmd}")
    return subprocess.run(cmd, shell=True, check=True, **kw)

def run_ok(cmd: str) -> bool:
    return run_q(cmd).returncode == 0

def has_cmd(name: str) -> bool:
    return shutil.which(name) is not None

# ══════════════════════════════════════════════════════════════════════════════
# State
# ══════════════════════════════════════════════════════════════════════════════

class S:
    install_type: str = ""           # single | distributed
    node_role: str = ""              # all-in-one | gsm | telegram
    action: str = "install"          # install | update | remove

    src_version: str = ""            # version from cloned repo
    installed_version: str = ""      # version currently on this machine
    version_ok: bool = True          # True if versions match or fresh install

    os_id: str = ""
    os_ver: str = ""
    pkg: str = ""                    # "dnf install -y {}"
    mgr: str = ""                    # dnf | apt-get
    svc_grp: str = ""

    python_ok: bool = False
    python_ver: str = ""
    has_ast: bool = False
    ast_ver: str = ""
    has_dongle: bool = False
    has_ts: bool = False
    ts_ip: str = ""

    tty_devs: List[str] = []
    usb_modems: List[str] = []

    node_id: str = ""
    modem_model: str = ""
    sim_phone: str = ""
    dongle_name: str = "gsm"
    ami_pw: str = ""
    tg_api_id: str = ""
    tg_api_hash: str = ""
    tg_username: str = ""
    agent_token: str = ""
    http_secret: str = ""
    own_ip: str = ""
    peer_ip: str = ""
    acl_ids: str = ""

    do_ts: bool = False              # install tailscale (distributed — required)
    do_ts_opt: bool = False          # install tailscale (single — optional)

    # Source path — where the repo ends up after clone
    src_dir: str = ""

    # Verification results — populated by phase_verify()
    verify_issues: List = []

    # Asterisk change tracking — set by _setup_ami() / _install_asterisk_dialplan()
    # so phase_start() can reload or restart Asterisk with the right urgency.
    ast_env_changed: bool = False     # unit EnvironmentFile changed -> restart
    ast_config_changed: bool = False  # dialplan/globals/AMI changed -> core reload

s = S()

# ══════════════════════════════════════════════════════════════════════════════
# Diagnostics
# ══════════════════════════════════════════════════════════════════════════════

def detect_os() -> None:
    info_: Dict[str, str] = {"id": "unknown", "version": "unknown"}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            k, _, v = line.partition("=")
            v = v.strip().strip('"')
            if k == "ID":
                info_["id"] = v
            elif k == "VERSION_ID":
                info_["version"] = v
    except FileNotFoundError:
        pass
    s.os_id = info_["id"]
    s.os_ver = info_["version"]

    if s.os_id in ("almalinux", "rhel", "centos", "rocky", "fedora"):
        s.pkg, s.mgr = "dnf install -y {}", "dnf"
    elif s.os_id in ("ubuntu", "debian"):
        s.pkg, s.mgr = "apt-get install -y {}", "apt-get"
    else:
        s.pkg, s.mgr = "", "unknown"

    try:
        grp.getgrnam("nogroup"); s.svc_grp = "nogroup"
    except KeyError:
        s.svc_grp = "simbridge"

def detect_python() -> None:
    if not has_cmd("python3"):
        s.python_ok, s.python_ver = False, "not installed"
        return
    r = run_q("python3 -c "
              '"import sys; print(f\'{sys.version_info.major}.{sys.version_info.minor}\')"')
    if r.returncode:
        s.python_ok, s.python_ver = False, r.stderr.strip() or "error"
        return
    parts = r.stdout.strip().split(".")
    s.python_ok = len(parts) == 2 and int(parts[0]) >= 3 and int(parts[1]) >= 9
    s.python_ver = r.stdout.strip()

def detect_asterisk() -> None:
    if not has_cmd("asterisk"):
        s.has_ast, s.ast_ver = False, ""
        return
    r = run_q("asterisk -rx 'core show version' 2>/dev/null")
    m = re.search(r"(\d+\.\d+)", r.stdout)
    s.has_ast = bool(m)
    s.ast_ver = m.group(1) if m else ""

def detect_dongle() -> None:
    if not has_cmd("asterisk"):
        s.has_dongle = False; return
    r = run_q("asterisk -rx 'module show like dongle' 2>/dev/null")
    s.has_dongle = "dongle" in r.stdout.lower()

def detect_tailscale() -> None:
    if not has_cmd("tailscale"):
        s.has_ts, s.ts_ip = False, ""
        return
    r = run_q("tailscale ip -4 2>/dev/null")
    s.has_ts = r.returncode == 0
    s.ts_ip = r.stdout.strip().split("\n")[0] if s.has_ts else ""

def detect_usb() -> None:
    s.tty_devs = []
    for d in sorted(Path("/dev").iterdir(), key=lambda p: p.name):
        if d.name.startswith(("ttyUSB", "ttyACM")):
            s.tty_devs.append(str(d))
    s.usb_modems = []
    if has_cmd("lsusb"):
        r = run_q("lsusb 2>/dev/null")
        kw = re.compile(r"huawei|dacom|quectel|simcom|telit|wavecom|gsm|modem", re.I)
        s.usb_modems = [l for l in r.stdout.splitlines() if kw.search(l)]

def detect_existing() -> Dict[str, Any]:
    svcs = []
    for sv in ("simbridge-agent", "simbridge-userbot"):
        if sv in run_q(f"systemctl list-unit-files {sv}.service 2>/dev/null").stdout:
            svcs.append(sv)
    paths = [p for p in (CONF_DIR, INSTALL_DIR, VENV_DIR, DATA_DIR, LOG_DIR)
             if Path(p).exists()]
    return {"found": bool(svcs or paths), "services": svcs, "paths": paths}

# ══════════════════════════════════════════════════════════════════════════════
# Phase 1 — Installation type
# ══════════════════════════════════════════════════════════════════════════════

def phase_type() -> None:
    heading("1 / 8 — Installation Type")
    c = pick("Deployment type:",
             ["Single-node (all-in-one)", "Two-node (distributed)"], 0)
    s.install_type = "single" if "Single" in c else "distributed"

    if s.install_type == "single":
        s.node_role = "all-in-one"
        info("All services on this machine.")
    else:
        c = pick("Role of THIS machine:",
                 ["GSM node (Asterisk + modem)", "Telegram node (userbot)"], 0)
        s.node_role = "gsm" if "GSM" in c else "telegram"
        info(f"Role: {s.node_role}")

# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 — Existing installation
# ══════════════════════════════════════════════════════════════════════════════

def phase_existing() -> None:
    heading("2 / 8 — Existing Installation")
    ex = detect_existing()
    if not ex["found"]:
        info("No existing installation found."); return

    info("Found:")
    if ex["services"]:
        info("  Services:", f" {', '.join(ex['services'])}")
    if ex["paths"]:
        info("  Paths:", f" {', '.join(ex['paths'])}")

    c = pick("Action:",
             ["Remove existing and start fresh", "Update in place", "Abort"], 1)
    if "Remove" in c:
        s.action = "remove"
    elif "Abort" in c:
        info("Aborted."); sys.exit(0)
    else:
        s.action = "update"
        info("Loading existing configuration for defaults...")
        _load_existing_config()

# ══════════════════════════════════════════════════════════════════════════════
# Phase 3 — Diagnostics
# ══════════════════════════════════════════════════════════════════════════════

def phase_diag() -> None:
    heading("3 / 8 — Environment Diagnostics")
    detect_os()
    detect_python()
    gsm = s.node_role in ("gsm", "all-in-one")
    if gsm:
        detect_asterisk(); detect_dongle()
    detect_tailscale(); detect_usb()

    info("OS:", f" {s.os_id} {s.os_ver}")
    info("Packages:", f" {s.mgr}")

    ok("Python", f" {s.python_ver}") if s.python_ok \
        else warn("Python 3.9+ needed (got", f" {s.python_ver})")

    if gsm:
        if s.has_ast:
            ok("Asterisk", f" {s.ast_ver}")
        else:
            warn("Asterisk not found — will install base package.")
        if s.has_dongle:
            ok("chan_dongle:", " loaded")
        else:
            warn("chan_dongle not detected.")
            warn("  AlmaLinux:", " https://wiringSoft.com/ for RPMs")
            warn("  Ubuntu:", "  sudo add-apt-repository ppa:dongle-project/ppa")

    if s.install_type == "distributed":
        if s.has_ts:
            ok("Tailscale:", f" {s.ts_ip}")
        else:
            warn("Tailscale not installed (required for distributed mode).")
            s.do_ts = ask_yn("Install Tailscale now?")
    else:
        if s.has_ts:
            ok("Tailscale:", f" {s.ts_ip}")
        else:
            s.do_ts_opt = ask_yn("Install Tailscale (recommended)?", False)

    if gsm:
        if s.tty_devs:
            ok("USB serial:", f" {', '.join(s.tty_devs)}")
        else:
            warn("No /dev/ttyUSB* or /dev/ttyACM* found.")

# ══════════════════════════════════════════════════════════════════════════════
# Phase 4 — Gather configuration
# ══════════════════════════════════════════════════════════════════════════════

def phase_gather() -> None:
    heading("4 / 8 — Configuration")
    _nid = s.node_id or os.uname().nodename
    s.node_id = ask("Node ID", default=_nid)

    gsm = s.node_role in ("gsm", "all-in-one")
    tg = s.node_role in ("telegram", "all-in-one")

    if gsm:
        info("", "--- GSM / Modem ---")
        modems = s.usb_modems
        if modems:
            choices = list(modems) + ["Other (manual entry)"]
            s.modem_model = pick("Select connected modem:", choices, 0)
            if s.modem_model == "Other (manual entry)":
                s.modem_model = ask("Enter modem model manually", required=True)
        else:
            warn("No USB modems detected via lsusb.")
            s.modem_model = ask("Enter modem model manually",
                                required=True, default=s.modem_model)
        s.sim_phone = ask("SIM phone number (e.g. +79991234567)",
                          default=s.sim_phone)
        s.dongle_name = ask("chan_dongle device name", default=s.dongle_name or "gsm")

        s.ami_pw = _ensure_ami()
        new_pw = ask("AMI password (empty = use auto-detected)", required=False)
        if new_pw:
            s.ami_pw = new_pw

    if tg:
        info("", "--- Telegram ---")
        s.tg_api_id = ask("Telegram API_ID (my.telegram.org/apps)",
                          default=s.tg_api_id)
        s.tg_api_hash = ask("Telegram API_HASH",
                            default=s.tg_api_hash)
        s.tg_username = ask("Telegram username (without @)",
                            default=s.tg_username)

    info("", "--- Secrets ---")
    if s.node_role in ("gsm", "all-in-one"):
        tok = ask("Agent token (empty = auto)",
                  required=False, default=s.agent_token)
        if not tok:
            tok = _rand(32)
            warn(f"Agent token: {tok}")
            warn("(Save — needed on Telegram node.)")
        s.agent_token = tok

    if tg:
        if s.node_role == "telegram":
            s.agent_token = ask("Agent token (must match GSM node)",
                                default=s.agent_token)
        sec = ask("HTTP secret (empty = auto)",
                  required=False, default=s.http_secret)
        if not sec:
            sec = _rand(32)
            warn(f"HTTP secret: {sec}")
        s.http_secret = sec

    if s.install_type == "distributed":
        info("", "--- Network ---")
        _own = s.ts_ip or s.own_ip or ""
        if not _own:
            _own = ask("This node's Tailscale IP")
        s.own_ip = _own
        if s.node_role == "gsm":
            s.peer_ip = ask("Telegram node Tailscale IP",
                            required=True, default=s.peer_ip)
        else:
            s.peer_ip = ask("GSM node Tailscale IP",
                            required=True, default=s.peer_ip)
    else:
        s.own_ip = "127.0.0.1"
        s.peer_ip = "127.0.0.1"

    info("", "--- ACL ---")
    s.acl_ids = ask(
        "Telegram user ID(s) (space-separated, e.g. 123456789; each gets admin access to all ops)",
        required=True, default=s.acl_ids)

# ══════════════════════════════════════════════════════════════════════════════
# Phase 5 — Remove / Install
# ══════════════════════════════════════════════════════════════════════════════

def phase_remove() -> None:
    if s.action != "remove":
        return
    heading("5a / 8 — Remove Existing")
    for svc in ("simbridge-agent", "simbridge-userbot", "simbridge-sweep"):
        if run_ok(f"systemctl is-active --quiet {svc}"):
            info(f"Stopping {svc}...")
            run_q(f"systemctl stop {svc}"); run_q(f"systemctl disable {svc}")
    for u in ("simbridge-agent.service", "simbridge-userbot.service",
              "simbridge-sweep.service", "simbridge-sweep.timer"):
        p = Path(f"/etc/systemd/system/{u}")
        if p.exists():
            p.unlink(); info("Removed:", f" {u}")
    # Our asterisk env drop-in (package files like extensions.conf stay)
    dropin = Path(AST_DROPIN)
    if dropin.exists():
        dropin.unlink()
        info("Removed:", AST_DROPIN)
        run_ok(f"rmdir {dropin.parent} 2>/dev/null")
    run_ok("systemctl daemon-reload")
    for d in (CONF_DIR, DATA_DIR, LOG_DIR, INSTALL_DIR, VENV_DIR):
        if Path(d).exists():
            shutil.rmtree(d, ignore_errors=True)
            info("Removed:", f" {d}")
    ok("Clean.")

def phase_install() -> None:
    heading("5b / 8 — Install")
    gsm = s.node_role in ("gsm", "all-in-one")
    tg = s.node_role in ("telegram", "all-in-one")

    # ── System packages ──
    info("Updating package cache... (may take a while, output follows below)")
    run(f"{s.mgr} update -y")
    run(s.pkg.format("python3 python3-pip git curl"))
    # python3-venv: needed on Debian/Ubuntu; bundled with python3 on RHEL 9+
    run_ok(s.pkg.format("python3-venv"))
    if gsm:
        run(s.pkg.format("asterisk"))
        run_ok(s.pkg.format("asterisk-addons"))
        # ffmpeg — voicemail loudnorm in core/voicemail_forward.py
        run_ok(s.pkg.format("ffmpeg"))

    # ── Tailscale ──
    if s.do_ts or s.do_ts_opt:
        _install_tailscale()

    # ── Clone the repo ──
    _clone_repo()

    # ── Version check ──
    s.src_version = _read_ver(s.src_dir)
    info(f"Source version: {s.src_version}")
    inst_ver = _read_ver(INSTALL_DIR)
    if inst_ver != "unknown":
        s.installed_version = inst_ver
        info(f"Installed version: {inst_ver}")
        if _ver_tuple(s.src_version) != _ver_tuple(inst_ver):
            s.version_ok = False
            warn(f"Version mismatch — updating from {inst_ver} to {s.src_version}.")
        else:
            ok("Versions match.")
    else:
        s.installed_version = "(none — fresh install)"
        info("Fresh installation.")

    # ── Service user ──
    run_ok(f"id {SVC_USER} || useradd --system --no-create-home "
           f"--shell /usr/sbin/nologin {SVC_USER}")
    run_ok(f"groupadd --system {s.svc_grp} 2>/dev/null")
    # asterisk reads SimBridge config/blacklist (0640, group-owned) — the
    # sweep timer and AGI hooks run as the asterisk user
    run_ok(f"usermod -aG {s.svc_grp} asterisk 2>/dev/null")

    # ── Directories ──
    for d in (CONF_DIR, DATA_DIR, LOG_DIR, f"{DATA_DIR}/recordings"):
        Path(d).mkdir(parents=True, exist_ok=True)

    # ── Asterisk AMI ──
    if gsm:
        _setup_ami()

    # ── Python venv + deps ──
    if not Path(VENV_DIR).is_dir():
        run(f"python3 -m venv {VENV_DIR}")
    pip = f"{VENV_DIR}/bin/pip"
    run(f"{pip} install --upgrade pip")
    if gsm:
        run(f"{pip} install -r {s.src_dir}/agent/requirements.txt")
    if tg:
        run(f"{pip} install -r {s.src_dir}/userbot/requirements.txt")
    ok("Python deps installed.")

    # ── Config ──
    _write_config()

    # ── Asterisk dialplan, globals, AGI hooks, prompt (GSM nodes) ──
    if gsm:
        _install_asterisk_dialplan()

    # ── systemd ──
    _install_systemd()

    # ── Permissions ──
    _set_perms()

    # ── Enable services ──
    _enable()

def _install_tailscale() -> None:
    info("Installing Tailscale...")
    if s.mgr == "dnf":
        run_ok("dnf config-manager --add-repo "
               "https://pkgs.tailscale.com/stable/fedora/$basearch.repo 2>/dev/null")
        run_ok("curl -fsSL https://packages.tailscale.com/stable/etc/rpm/gpg.key "
               "| rpm --import - 2>/dev/null")
    else:
        run_ok("curl -fsSL https://packages.tailscale.com/stable.gpg "
               "| tee /etc/apt/keyrings/tailscale.gpg &>/dev/null")
        Path("/etc/apt/sources.list.d/tailscale.list").write_text(
            f"deb [signed-by=/etc/apt/keyrings/tailscale.gpg] "
            f"https://pkgs.tailscale.com/stable/{s.os_id} main\n")
        run(f"{s.mgr} update -y")
    run(s.pkg.format("tailscale"))
    run_ok("systemctl enable --now tailscaled")

def _clone_repo() -> None:
    """Clone the SimBridge repo into a staging directory.

    The clone lives at /tmp/simbridge-clone-XXXX so we don't pollute
    the production path until everything is verified.
    """
    heading("Cloning SimBridge Repository")
    s.src_dir = tempfile.mkdtemp(prefix="simbridge-clone-", suffix=str(os.getpid()))
    info("Staging:", f" {s.src_dir}")

    # Prefer HTTPS — works without SSH keys
    repo = REPO
    ref = f"{BRANCH} --depth 1"
    try:
        run(f"git clone --branch {ref} -- {repo} {s.src_dir}")
        ok("Cloned from HTTPS.")
    except subprocess.CalledProcessError:
        warn("HTTPS clone failed — trying SSH...")
        repo = SSH_REPO
        ref = f"{BRANCH}"
        try:
            run(f"git clone --branch {ref} -- {repo} {s.src_dir}")
            ok("Cloned from SSH.")
        except subprocess.CalledProcessError:
            fail("Both clone methods failed.")
            fail("Check network and access to the repository.")
            sys.exit(1)

    # Copy required trees into production
    info("Deploying to", f" {INSTALL_DIR}...")
    Path(INSTALL_DIR).mkdir(parents=True, exist_ok=True)
    # Copy sub-directories
    for sub in ("agent", "userbot", "core", "bridge", "config", "deploy",
                "scripts", "asterisk", "sounds"):
        src = Path(s.src_dir) / sub
        if src.exists():
            dst = Path(INSTALL_DIR) / sub
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)

    # Copy VERSION file into production
    ver_src = Path(s.src_dir) / "VERSION"
    if ver_src.exists():
        shutil.copy(str(ver_src), INSTALL_DIR)
    ok("Application deployed.", f" v{s.src_version}")

def _setup_ami() -> None:
    """Configure Asterisk AMI for the simbridge user.

    The secret lives ONLY in manager_custom.conf (survives package updates
    to manager.conf). manager.conf is patched to enable AMI and Include the
    custom file; a legacy inline [simbridge] section (written by older
    installer versions) is removed so the password has one source of truth.
    """
    custom = Path(f"{AST_DIR}/manager_custom.conf")
    main_conf = Path(f"{AST_DIR}/manager.conf")
    main_conf.parent.mkdir(parents=True, exist_ok=True)

    # ── Secret: manager_custom.conf (single source of truth) ──
    custom_cfg = (
        "[simbridge]\n"
        f"secret = {s.ami_pw}\n"
        "deny = 0.0.0.0/0.0.0.0\n"
        "permit = 127.0.0.1/255.255.255.0\n"
        "read = all\n"
        "write = all\n"
    )
    try:
        if custom.read_text() == custom_cfg:
            ok("AMI configured.", str(custom))
        else:
            custom.write_text(custom_cfg)
            _chown_asterisk(custom)
            custom.chmod(0o640)
            s.ast_config_changed = True
            ok("AMI user configured.", str(custom))
    except FileNotFoundError:
        custom.write_text(custom_cfg)
        _chown_asterisk(custom)
        custom.chmod(0o640)
        s.ast_config_changed = True
        ok("AMI user configured.", str(custom))

    # ── manager.conf: enabled=yes + Include, no legacy inline section ──
    if not main_conf.exists():
        main_conf.write_text(
            "[general]\n"
            "enabled = yes\n"
            "port = 5038\n"
            "bindaddr = 127.0.0.1\n"
            "Include manager_custom.conf\n"
        )
        _chown_asterisk(main_conf)
        main_conf.chmod(0o640)
        s.ast_config_changed = True
        ok("manager.conf created.")
        return

    txt = main_conf.read_text()
    orig = txt
    # Older installers put the secret inline in manager.conf — drop it.
    txt = _strip_section(txt, "simbridge")
    if "enabled = yes" not in txt:
        if re.search(r"^enabled\s*=", txt, flags=re.MULTILINE):
            txt = re.sub(r"^enabled\s*=\s*\S+", "enabled = yes",
                         txt, count=1, flags=re.MULTILINE)
        elif "[general]" in txt:
            txt = txt.replace("[general]", "[general]\nenabled = yes", 1)
        else:
            txt = txt.rstrip("\n") + "\n[general]\nenabled = yes\n"
    include_line = "Include manager_custom.conf"
    if include_line not in txt:
        txt = txt.rstrip("\n") + "\n" + include_line + "\n"
    if txt != orig:
        main_conf.write_text(txt)
        _chown_asterisk(main_conf)
        main_conf.chmod(0o640)
        s.ast_config_changed = True
        ok("manager.conf patched.")
    else:
        ok("manager.conf OK.")

def _write_config() -> None:
    heading("Writing Configuration")
    if s.install_type == "single":
        al, ul, vh = "127.0.0.1:8090", "127.0.0.1:8088", "127.0.0.1"
    else:
        al = f"{s.own_ip}:8090"
        ul = f"{s.own_ip}:8088"
        vh = s.peer_ip

    yaml = f"""\
# SimBridge — generated by install.py
node:
  role: {s.node_role}
  id: {s.node_id}
telegram:
  master_username: "{s.tg_username or '<telegram_username>'}"
  session_path: {DATA_DIR}/sim_session
  acl_file: {ACL_FILE}
  api_id_env: SIMBRIDGE_TG_API_ID
  api_hash_env: SIMBRIDGE_TG_API_HASH
agent:
  listen: "{al}"
  token_env: SIMBRIDGE_AGENT_TOKEN
  allowed_peers:
    - "{s.peer_ip}"
userbot_http:
  listen: "{ul}"
  secret_env: SIMBRIDGE_HTTP_SECRET
  allowed_peers:
    - "{s.peer_ip}"
asterisk:
  ari_url: http://127.0.0.1:8088/ari
  dongle: {s.dongle_name}
  ring_wait_seconds: 24
  max_record_seconds: 90
  early_hangup_max_seconds: 3
  sweep_max_age_seconds: 300
  prompt: {AST_PROMPT}
  ami_host: 127.0.0.1
  ami_port: 5038
  ami_username: simbridge
  ami_password_env: SIMBRIDGE_AMI_PASSWORD
sim:
  phone: "{s.sim_phone}"
  modem_model: "{s.modem_model}"
voice:
  bridge_endpoint: tg-bridge
  bridge_host: {vh}
  bridge_port: 5062
  srtp: false
  outbound_answer_timeout: 30
limits:
  sms_per_hour: 30
  calls_per_minute: 3
  max_call_seconds: 3600
paths:
  blacklist: {BLACKLIST_FILE}
  contacts_cache: {DATA_DIR}/contacts.csv
  audit_log: {LOG_DIR}/audit.jsonl
  recordings_dir: {DATA_DIR}/recordings
"""
    # Back up a different existing config on updates (Rule 4 — the file
    # may hold hand-tuned values the round-trip parser cannot preserve).
    if s.action == "update" and Path(CONF_FILE).exists():
        old_cfg = Path(CONF_FILE).read_text()
        if old_cfg != yaml:
            bak = f"{CONF_FILE}.bak-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
            Path(bak).write_text(old_cfg)
            warn("Existing config backed up:", f" {bak}")
    Path(CONF_FILE).write_text(yaml); Path(CONF_FILE).chmod(0o640)

    # ACL
    if not Path(ACL_FILE).exists():
        lines = ["# SimBridge ACL: <user_id> <right1> <right2> ...",
                 "# Rights: in_sms in_call out_sms out_call"]
        for uid in s.acl_ids.split():
            lines.append(f"{uid} out_sms in_sms out_call in_call")
        Path(ACL_FILE).write_text("\n".join(lines) + "\n"); Path(ACL_FILE).chmod(0o640)
        ok("ACL:", ACL_FILE)

    # Blacklist
    bl = Path(BLACKLIST_FILE)
    old_bl = Path("/etc/asterisk/blacklist/numbers.txt")  # pre-AGI dialplan
    if not bl.exists() and old_bl.exists():
        shutil.copy(str(old_bl), str(bl))
        info("Migrated blacklist:", f" {old_bl} -> {bl}")
    if not bl.exists():
        ex = Path(f"{s.src_dir}/config/blacklist.example.txt")
        if ex.exists():
            shutil.copy(str(ex), str(bl))
        else:
            bl.write_text("# Blocked numbers (E.164, one/line)\n")
    # 0640: the AGI blacklist check runs as asterisk (service-group member)
    bl.chmod(0o640)

    # Secrets — merge, never clobber (host may carry keys added by hand)
    _merge_env()
    ok("Config:", CONF_FILE)
    ok("Secrets:", ENV_FILE)

def _merge_env() -> None:
    """Merge collected secrets into ENV_FILE — never clobber.

    Existing keys (including ones added by hand) keep their position,
    extra keys survive, collected values update their keys in place,
    and new keys are appended at the end. Stays chmod 0600 (Rule 5).
    """
    p = Path(ENV_FILE)
    wanted: Dict[str, str] = {}
    if s.agent_token:
        wanted["SIMBRIDGE_AGENT_TOKEN"] = s.agent_token
    if s.tg_api_id and s.tg_api_hash:
        wanted["SIMBRIDGE_TG_API_ID"] = s.tg_api_id
        wanted["SIMBRIDGE_TG_API_HASH"] = s.tg_api_hash
    if s.http_secret:
        wanted["SIMBRIDGE_HTTP_SECRET"] = s.http_secret
    if s.ami_pw:
        wanted["SIMBRIDGE_AMI_PASSWORD"] = s.ami_pw
    # notify-agent-agi.py (S04) reads the agent URL from the environment —
    # the same value the YAML template gives to agent.listen (Rule 1).
    if s.node_role in ("gsm", "all-in-one"):
        if s.install_type == "single":
            wanted["AGENT_URL"] = "http://127.0.0.1:8090"
        elif s.own_ip:
            wanted["AGENT_URL"] = f"http://{s.own_ip}:8090"

    raw = p.read_text().splitlines() if p.exists() else []
    if not raw:
        lines = ["# SimBridge secrets — NEVER commit (Rule 5)", ""]
        lines += [f"{k} = {v}" for k, v in wanted.items()]
        p.write_text("\n".join(lines) + "\n")
        p.chmod(0o600)
        ok("Secrets written.", ENV_FILE)
        return

    seen: set = set()
    lines: List[str] = []
    changed = 0
    for line in raw:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, v = stripped.partition("=")
            k = k.strip()
            if k in wanted:
                seen.add(k)
                if v.strip() != wanted[k]:
                    changed += 1
                lines.append(f"{k} = {wanted[k]}")
                continue
        lines.append(line)

    added = [k for k in wanted if k not in seen]
    if added:
        if lines and lines[-1].strip():
            lines.append("")
        for k in added:
            lines.append(f"{k} = {wanted[k]}")

    p.write_text("\n".join(lines) + "\n")
    p.chmod(0o600)
    if changed or added:
        info("Secrets updated:",
             f" {changed} changed, {len(added)} new -> {ENV_FILE}")
    else:
        ok("Secrets unchanged.", ENV_FILE)

def _chown_asterisk(p: Path) -> None:
    """Chown a file to the asterisk user (uid/gid looked up by name)."""
    try:
        st = pwd.getpwnam("asterisk")
        p.chown(st.pw_uid, st.pw_gid)
    except (KeyError, OSError):
        pass

def _agi_bin_dir() -> str:
    """Asterisk's AGI application dir (compile-time constant per package)."""
    for d in AGI_BIN_DIRS:
        if Path(d).is_dir():
            return d
    return AGI_BIN_DIRS[0]

def _strip_section(txt: str, name: str) -> str:
    """Remove a '[name]' section from Asterisk config text (legacy cleanup)."""
    out: List[str] = []
    skip = False
    for ln in txt.splitlines():
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            skip = s[1:-1].strip().lower() == name
        if not skip:
            out.append(ln)
    return "\n".join(out) + "\n"

def _install_asterisk_dialplan() -> None:
    """Install dialplan, generated globals, VM prompt and AGI hooks.

    Everything is written BEFORE Asterisk is (re)loaded: extensions.conf
    #includes asterisk-globals.conf, which must exist at load time.
    phase_start() applies the reload (config) or restart (environment).
    """
    heading("Installing Asterisk Dialplan")

    # 1. Dialplan — extensions.conf from the repo
    ext_src = Path(s.src_dir) / "asterisk" / "extensions.conf"
    if not ext_src.exists():
        fail("Dialplan missing in repo:", str(ext_src))
        return
    dst = Path(AST_EXTENSIONS)
    new_txt = ext_src.read_text()
    old_txt = dst.read_text() if dst.exists() else None
    if old_txt != new_txt:
        if old_txt is not None:
            bak = (f"{AST_EXTENSIONS}.bak-"
                   f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}")
            Path(bak).write_text(old_txt)
            warn("Existing dialplan backed up:", bak)
        dst.write_text(new_txt)
        dst.chmod(0o644)
        _chown_asterisk(dst)
        s.ast_config_changed = True
        ok("Dialplan:", AST_EXTENSIONS)
    else:
        ok("Dialplan unchanged.", AST_EXTENSIONS)

    # 2. Globals — generated from the config _write_config() just wrote
    gen = (f"{VENV_DIR}/bin/python {INSTALL_DIR}/scripts/"
           f"generate_asterisk_config.py {CONF_FILE} -o {AST_GLOBALS}")
    old_globals = (Path(AST_GLOBALS).read_text()
                   if Path(AST_GLOBALS).exists() else None)
    if run_ok(gen):
        _chown_asterisk(Path(AST_GLOBALS))
        if Path(AST_GLOBALS).read_text() != old_globals:
            s.ast_config_changed = True
            ok("Globals:", AST_GLOBALS)
        else:
            ok("Globals unchanged.", AST_GLOBALS)
    else:
        fail("Globals generation failed — needs the agent venv (PyYAML).")
        fail("Command:", gen)

    # 3. Voicemail prompt — only overwritten when content differs
    snd_src = Path(s.src_dir) / "sounds" / "vm-prompt.ulaw"
    snd_dst = Path(AST_PROMPT)
    if snd_src.exists():
        snd_dst.parent.mkdir(parents=True, exist_ok=True)
        if not snd_dst.exists() or snd_dst.read_bytes() != snd_src.read_bytes():
            shutil.copy(str(snd_src), str(snd_dst))
            ok("Prompt:", str(snd_dst))
        snd_dst.chmod(0o644)
        _chown_asterisk(snd_dst)
    else:
        warn("Prompt missing in repo:", str(snd_src))

    # 4. AGI hooks — exec bit + symlink into Asterisk's AGI bin dir
    agi_dir = _agi_bin_dir()
    Path(agi_dir).mkdir(parents=True, exist_ok=True)
    for name in AGI_SCRIPTS:
        app = Path(INSTALL_DIR) / "scripts" / name
        if not app.exists():
            warn("AGI script missing:", str(app))
            continue
        app.chmod(0o755)
        link = Path(agi_dir) / name
        if link.is_symlink() and link.resolve() == app.resolve():
            continue
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(str(app))
            ok("AGI linked:", name)
        except OSError as e:
            warn(f"AGI link {name} failed:", str(e))

    # 5. Asterisk env drop-in — AGI hooks inherit /etc/simbridge/env
    #    (Rule 5: secrets never in unit files or dialplan)
    dropin_txt = ("[Service]\n"
                  "EnvironmentFile=/etc/simbridge/env\n"
                  "Environment=SIMBRIDGE_CONFIG=/etc/simbridge/simbridge.yaml\n")
    dropin = Path(AST_DROPIN)
    if not dropin.exists() or dropin.read_text() != dropin_txt:
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text(dropin_txt)
        run_ok("systemctl daemon-reload")
        s.ast_env_changed = True
        ok("Asterisk env drop-in:", AST_DROPIN)
        warn("AGI hooks now read secrets from Asterisk's process environment.")
        warn("Phase 7 will restart Asterisk — active calls will be dropped.")
    else:
        ok("Asterisk env drop-in unchanged.")

def _install_systemd() -> None:
    heading("Installing systemd Units")
    sd = Path(f"{s.src_dir}/deploy/systemd")
    if not sd.is_dir():
        sd = Path(f"{INSTALL_DIR}/deploy/systemd")
    gsm = s.node_role in ("gsm", "all-in-one")
    tg = s.node_role in ("telegram", "all-in-one")

    # Render placeholders (e.g. __SVC_GROUP__ -> nogroup/nobody/simbridge)
    def _render(src: Path, dst: str) -> None:
        text = src.read_text()
        text = text.replace("__SVC_GROUP__", s.svc_grp)
        Path(dst).write_text(text)

    if gsm and (sd / "simbridge-agent.service").exists():
        _render(sd / "simbridge-agent.service",
                "/etc/systemd/system/simbridge-agent.service")
        ok("simbridge-agent.service")
    if gsm and (sd / "simbridge-sweep.service").exists():
        _render(sd / "simbridge-sweep.service",
                "/etc/systemd/system/simbridge-sweep.service")
        ok("simbridge-sweep.service")
    if gsm and (sd / "simbridge-sweep.timer").exists():
        _render(sd / "simbridge-sweep.timer",
                "/etc/systemd/system/simbridge-sweep.timer")
        ok("simbridge-sweep.timer")
    if tg and (sd / "simbridge-userbot.service").exists():
        _render(sd / "simbridge-userbot.service",
                "/etc/systemd/system/simbridge-userbot.service")
        ok("simbridge-userbot.service")
    run_ok("systemctl daemon-reload")

def _chown(p: str, rec: bool = False) -> None:
    try:
        shutil.chown(p, user=SVC_USER, group=s.svc_grp)
        if rec and Path(p).is_dir():
            for x in Path(p).rglob("*"):
                shutil.chown(str(x), user=SVC_USER, group=s.svc_grp)
    except (OSError, LookupError):
        pass

def _set_perms() -> None:
    heading("Setting Permissions")
    _chown(INSTALL_DIR, rec=True)
    _chown(VENV_DIR, rec=True)
    _chown(CONF_DIR, rec=True); _chown(DATA_DIR, rec=True); _chown(LOG_DIR, rec=True)
    # Recordings: written by Asterisk (MixMonitor) and swept by the
    # simbridge-sweep timer — both run as the asterisk user, so the
    # recursive simbridge chown above must not stick (see the unit docs).
    if s.node_role in ("gsm", "all-in-one"):
        _chown_asterisk(Path(f"{DATA_DIR}/recordings"))
    Path(CONF_FILE).chmod(0o640)
    Path(ENV_FILE).chmod(0o600)
    sess = Path(f"{DATA_DIR}/sim_session.session")
    if sess.exists():
        _chown(str(sess)); sess.chmod(0o600)
    ok("Done.")

def _enable() -> None:
    heading("Enabling Services")
    gsm = s.node_role in ("gsm", "all-in-one")
    tg = s.node_role in ("telegram", "all-in-one")
    if s.node_role == "all-in-one":
        run_ok("systemctl enable simbridge-agent")
        run_ok("systemctl enable --now simbridge-sweep.timer")
        run_ok("systemctl enable simbridge-userbot")
        info("Enabled but NOT started — after Telegram login.")
    elif gsm:
        run_ok("systemctl enable simbridge-agent")
        run_ok("systemctl enable --now simbridge-sweep.timer")
        if not run_ok("systemctl start simbridge-agent"):
            warn("Agent start deferred (Asterisk/chan_dongle?).")
    elif tg:
        run_ok("systemctl enable simbridge-userbot")
        info("Enabled but NOT started — after Telegram login.")

# ══════════════════════════════════════════════════════════════════════════════
# Phase 6 — Telegram login
# ══════════════════════════════════════════════════════════════════════════════

def phase_telegram() -> None:
    if s.node_role not in ("telegram", "all-in-one"):
        return
    heading("6 / 8 — Telegram Login")
    info("Authenticates the userbot with Telegram.")
    info(f"Session file: {DATA_DIR}/sim_session.session")
    if not ask_yn("Log in now?"):
        warn("Skipped — do it manually before starting the userbot."); return

    info("Prompt:", f" {SVC_USER}...")
    info("You will be asked for phone number and code.")

    script = textwrap.dedent(
        "\nfrom telethon import TelegramClient\n"
        "c = TelegramClient({sess!r}, int({api!r}), {hash!r})\n"
        "with c:\n"
        "    me = c.get_me()\n"
        "    print(f'Logged in: {{me.first_name}} (@{{me.username or \"n/a\"}})'\n"
        ")\n").format(sess=f"{DATA_DIR}/sim_session",
                     api=s.tg_api_id, hash=s.tg_api_hash)

    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(script); tmp = f.name

    try:
        env = os.environ.copy()
        env["HOME"] = DATA_DIR
        env["PYTHONUNBUFFERED"] = "1"
        subprocess.run(
            ["sudo", "-u", SVC_USER, "--preserve-env=HOME,PYTHONUNBUFFERED",
             f"{VENV_DIR}/bin/python", tmp],
            env=env, timeout=300, check=True)
        ok("Telegram login successful.")
    except subprocess.TimeoutExpired:
        warn("Telegram login timed out.")
    except subprocess.CalledProcessError as e:
        warn(f"Telegram login failed (exit code {e.returncode}).")
    except FileNotFoundError:
        warn("sudo not found.")
    finally:
        os.unlink(tmp)

    if Path(f"{DATA_DIR}/sim_session.session").exists():
        ok("Session created.")
    else:
        warn("Session not created — login may have failed.")

# ══════════════════════════════════════════════════════════════════════════════
# Phase 7 — Start
# ══════════════════════════════════════════════════════════════════════════════

def phase_start() -> None:
    heading("7 / 8 — Starting Services")
    gsm = s.node_role in ("gsm", "all-in-one")
    tg = s.node_role in ("telegram", "all-in-one")
    update = s.action == "update"

    if gsm:
        # ── Ensure asterisk.conf exists (RHEL/Alma bug: package drops it) ──
        ast_conf = Path("/etc/asterisk/asterisk.conf")
        if not ast_conf.exists():
            warn("Missing /etc/asterisk/asterisk.conf — creating defaults.")
            _write_default_asterisk_conf()

        # ── Asterisk ──
        if not run_ok("systemctl is-active --quiet asterisk"):
            info("Starting Asterisk...")
            if run_ok("systemctl start asterisk"):
                ok("Asterisk started.")
            else:
                fail("Asterisk failed to start.")
                r = run_q("systemctl status asterisk --no-pager 2>&1 | tail -8")
                _w(r.stdout)
                info("Check logs: sudo journalctl -u asterisk --no-pager -n 20")
        elif s.ast_env_changed:
            # EnvironmentFile is read at process start — a full restart is
            # required. Active calls will be dropped.
            warn("Asterisk environment changed — restarting "
                 "(active calls will be dropped).")
            if run_ok("systemctl restart asterisk"):
                ok("Asterisk restarted.")
            else:
                fail("Asterisk restart failed — "
                     "journalctl -u asterisk --no-pager -n 30")
        elif s.ast_config_changed:
            # Config-only change: core reload keeps active calls alive.
            info("Reloading Asterisk config (non-disruptive)...")
            r = run_q("asterisk -rx 'core reload' 2>&1")
            if r.returncode == 0:
                ok("Asterisk config reloaded.")
            else:
                warn("core reload failed:", r.stdout.strip()[:120])
        else:
            ok("Asterisk already running.")

        # ── simbridge-agent ──
        if update and run_ok("systemctl is-active --quiet simbridge-agent"):
            info("Restarting simbridge-agent (update)...")
            if not run_ok("systemctl restart simbridge-agent"):
                warn("Agent restart failed — will be rechecked in verification.")
        elif not run_ok("systemctl is-active --quiet simbridge-agent"):
            info("Starting simbridge-agent...")
            if not run_ok("systemctl start simbridge-agent"):
                warn("Agent failed to start — will be rechecked in verification.")
        else:
            ok("simbridge-agent already running.")

    if tg:
        sess = Path(f"{DATA_DIR}/sim_session.session")
        if not sess.exists():
            warn("Telegram session file not found — userbot cannot start.")
            info(f"Re-run installer and choose 'Log in now' in Phase 6.")
        elif update and run_ok("systemctl is-active --quiet simbridge-userbot"):
            info("Restarting simbridge-userbot (update)...")
            if not run_ok("systemctl restart simbridge-userbot"):
                warn("Userbot restart failed — will be rechecked in verification.")
        elif not run_ok("systemctl is-active --quiet simbridge-userbot"):
            info("Starting simbridge-userbot...")
            if not run_ok("systemctl start simbridge-userbot"):
                warn("Userbot failed to start — will be rechecked in verification.")
        else:
            ok("simbridge-userbot already running.")

# ══════════════════════════════════════════════════════════════════════════════
# Phase 8 — Verify + Test
# ══════════════════════════════════════════════════════════════════════════════

def _check(label: str, ok_: bool, detail: str = "", fix: str = "") -> None:
    """Run one verification check and report result + remediation."""
    if ok_:
        ok(f"[OK] {label}")
    else:
        fail(f"[FAIL] {label}")
        if detail:
            _w(detail)
        if fix:
            info("Fix:", fix)

    s.verify_issues.append((label, not ok_, fix))


def phase_verify() -> None:
    heading("8 / 8 — Verification")
    gsm = s.node_role in ("gsm", "all-in-one")
    tg = s.node_role in ("telegram", "all-in-one")

    s.verify_issues = []  # (label, failed, fix_command)

    # ═══ GSM checks ═══
    if gsm:
        _w(f"\n  {'─' * 50}")
        _w(f"  Checking GSM services (node: {s.node_id})")
        _w(f"  {'─' * 50}\n")

        # 1. simbridge-agent
        r = run_q("systemctl is-active simbridge-agent 2>&1")
        agent_active = r.stdout.strip() == "active"
        if not agent_active:
            r2 = run_q("systemctl status simbridge-agent --no-pager 2>&1 | tail -8")
            detail = r2.stdout.strip().replace("\n", "\n    ")
            fix = ("journalctl -u simbridge-agent -n 30 --no-pager\n"
                   "systemctl start simbridge-agent")
        else:
            detail = ""; fix = ""
        _check("simbridge-agent service", agent_active,
               f"    {detail}", fix)

        # 2. Asterisk
        r = run_q("systemctl is-active asterisk 2>&1")
        ast_active = r.stdout.strip() == "active"
        if not ast_active:
            r2 = run_q("systemctl status asterisk --no-pager 2>&1 | tail -8")
            detail = r2.stdout.strip().replace("\n", "\n    ")
            # Check if config file is missing (common RHEL/Alma issue)
            if not Path("/etc/asterisk/asterisk.conf").exists():
                fix = ("Create /etc/asterisk/asterisk.conf from package defaults,\n"
                       "then: systemctl start asterisk")
            else:
                fix = ("journalctl -u asterisk -n 30 --no-pager\n"
                       "systemctl start asterisk")
        else:
            detail = ""; fix = ""
        _check("Asterisk service", ast_active,
               f"    {detail}", fix)

        # 3. chan_dongle module (only meaningful if Asterisk is running)
        if ast_active:
            r = run_q("asterisk -rx 'module show like dongle' 2>/dev/null")
            dongle_loaded = "dongle" in r.stdout.lower() and "active" in r.stdout.lower()
            _check("chan_dongle module loaded", dongle_loaded,
                   f"    Output: {r.stdout.strip()[:200]}",
                   "asterisk -rx 'module load chan_dongle.so'")

            # 4. Dongle status
            if dongle_loaded:
                r = run_q("asterisk -rx 'dongle show status' 2>/dev/null")
                has_status = r.returncode == 0 and "No such command" not in r.stdout
                if not has_status:
                    detail = r.stdout.strip().replace("\n", "\n    ")
                    fix = ("Check chan_dongle version. Command may be:\n"
                           "  asterisk -rx 'core show help' | grep dongle")
                else:
                    detail = r.stdout.strip().replace("\n", "\n    ")
                _check("Dongle modem status", has_status,
                       f"    {detail}", fix)
        else:
            warn("[SKIP] chan_dongle / modem status — Asterisk not running")
            s.verify_issues.append(
                ("chan_dongle / modem status (SKIP: Asterisk down)", True, ""))

        # 5. Agent health endpoint
        url = ("http://127.0.0.1:8090/v1/health"
               if s.install_type == "single"
               else f"http://{s.own_ip}:8090/v1/health")
        r = run_q(f'curl -sf --max-time 5 {url} '
                  f'-H "Authorization: Bearer {s.agent_token}"')
        health_ok = r.returncode == 0
        if not health_ok and agent_active:
            detail = (f"    URL: {url}\n"
                      f"    Response: {r.stderr.strip()[:300] or r.stdout.strip()[:300]}")
            fix = ("Check agent logs: journalctl -u simbridge-agent -n 20 --no-pager\n"
                   "Check config: cat /etc/simbridge/simbridge.yaml")
        elif not health_ok and not agent_active:
            detail = "    Agent not running — health check cannot succeed"
            fix = ""
        else:
            detail = ""; fix = ""
        _check("Agent health endpoint (" + url + ")", health_ok,
               detail, fix)

        # 6. Sweep timer — orphan voicemail recording safety net
        r = run_q("systemctl is-active simbridge-sweep.timer 2>&1")
        timer_ok = r.stdout.strip() == "active"
        if timer_ok:
            detail = ""; fix = ""
        else:
            detail = "    Orphan voicemail recordings will not be forwarded."
            fix = "systemctl enable --now simbridge-sweep.timer"
        _check("simbridge-sweep.timer (orphan recordings)", timer_ok,
               detail, fix)

        # 7. Generated globals — the dialplan #includes this file
        globals_ok = Path(AST_GLOBALS).exists()
        if globals_ok:
            detail = ""; fix = ""
        else:
            detail = f"    {AST_EXTENSIONS} #includes asterisk-globals.conf"
            fix = (f"{VENV_DIR}/bin/python {INSTALL_DIR}/scripts/"
                   f"generate_asterisk_config.py {CONF_FILE} -o {AST_GLOBALS}")
        _check("Asterisk globals file", globals_ok, detail, fix)

        # 8. AGI hooks reachable from Asterisk
        agi_dir = _agi_bin_dir()
        missing = [n for n in AGI_SCRIPTS if not (Path(agi_dir) / n).exists()]
        _check(f"AGI hooks in {agi_dir}", not missing,
               f"    Missing: {', '.join(missing)}" if missing else "",
               "Re-run the installer (AGI link step)")

    # ═══ Telegram checks ═══
    if tg:
        _w(f"\n  {'─' * 50}")
        _w(f"  Checking Telegram services (node: {s.node_id})")
        _w(f"  {'─' * 50}\n")

        # 1. Session file
        sess = Path(f"{DATA_DIR}/sim_session.session")
        sess_ok = sess.exists()
        if not sess_ok:
            fix = ("Re-run installer and complete Telegram login in Phase 6")
        else:
            fix = ""
        _check("Telegram session file", sess_ok,
               f"    Path: {DATA_DIR}/sim_session.session", fix)

        # 2. simbridge-userbot service
        r = run_q("systemctl is-active simbridge-userbot 2>&1")
        bot_active = r.stdout.strip() == "active"
        if not bot_active and not sess_ok:
            detail = "    Cannot start — Telegram session missing"
            fix = ""
        elif not bot_active:
            r2 = run_q("systemctl status simbridge-userbot --no-pager 2>&1 | tail -8")
            detail = r2.stdout.strip().replace("\n", "\n    ")
            fix = ("journalctl -u simbridge-userbot -n 30 --no-pager\n"
                   "systemctl start simbridge-userbot")
        else:
            detail = ""; fix = ""
        _check("simbridge-userbot service", bot_active, detail, fix)

    # ═══ Cross-node connectivity (distributed) ═══
    if s.install_type == "distributed":
        _w(f"\n  {'─' * 50}")
        _w(f"  Cross-node connectivity")
        _w(f"  {'─' * 50}\n")

        if gsm:
            peer_label = "Telegram"
        else:
            peer_label = "GSM"

        info(f"Peer ({peer_label} node):", f" {s.peer_ip}")
        info("", "  This check requires the peer node to be installed and running.")

        if ask_yn(f"Is the {peer_label} node ({s.peer_ip}) up and running?"):
            r = run_q(f'curl -sf --max-time 10 http://{s.peer_ip}:8090/v1/health '
                      f'-H "Authorization: Bearer {s.agent_token}"')
            if r.returncode == 0:
                _check(f"Connectivity to {peer_label} node ({s.peer_ip})", True)
            else:
                detail = (f"    curl returned exit code {r.returncode}\n"
                          f"    stderr: {r.stderr.strip()[:200]}")
                fix = (f"1. Verify {peer_label} node is on Tailscale: tailscale status\n"
                       f"2. Check agent is running: systemctl status simbridge-agent\n"
                       f"3. Verify token matches on both nodes\n"
                       f"4. Test manually:\n"
                       f"   curl -v http://{s.peer_ip}:8090/v1/health "
                       f'-H "Authorization: Bearer {{token}}"')
                _check(f"Connectivity to {peer_label} node ({s.peer_ip})", False,
                       detail, fix)
        else:
            info("", f"  Skipping — peer node ({peer_label}, {s.peer_ip}) not ready.")
            s.verify_issues.append(
                (f"Cross-node connectivity to {peer_label} ({s.peer_ip}) — SKIPPED",
                 False, ""))

    # ═══ Summary ═══
    _w()
    heading("Verification Result")

    failures = [(l, f) for l, failed, f in s.verify_issues if failed]
    skips = [(l, f) for l, failed, f in s.verify_issues
             if not failed and "SKIP" in l.upper()]
    passes = [(l, f) for l, failed, f in s.verify_issues
              if not failed and "SKIP" not in l.upper()]

    if passes:
        _w(f"  {C.G}Passed ({len(passes)}):{C._0}")
        for l, _ in passes:
            _w(f"    {C.G}+{C._0} {l}")

    if failures:
        _w(f"\n  {C.R}Failed ({len(failures)}):{C._0}")
        for l, f in failures:
            _w(f"    {C.R}x{C._0} {l}")
            if f:
                _w(f"       Fix: {f.split(chr(10))[0]}")

    if skips:
        _w(f"\n  {C.Y}Skipped ({len(skips)}):{C._0}")
        for l, _ in skips:
            _w(f"    {C.Y}-{C._0} {l}")

    if not s.verify_issues:
        info("No checks were performed (role may not require verification).")

    if failures:
        _w()
        fail(f"Installation has {len(failures)} issue(s) that need attention.")
        _w()
        info("Useful commands:", f" (node: {s.node_id})")
        if gsm:
            info("  Agent logs:", " journalctl -u simbridge-agent -f --no-pager")
            info("  Asterisk logs:", " journalctl -u asterisk -f --no-pager")
            info("  Asterisk CLI:", " asterisk -cv")
        if tg:
            info("  Userbot logs:", " journalctl -u simbridge-userbot -f --no-pager")
        info("  Config:", f" cat {CONF_FILE}")
        info("  Secrets:", f" cat {ENV_FILE}")
    elif skips and not failures:
        _w()
        warn("Some checks were skipped — complete them when peer node is ready.")
    else:
        _w()
        ok("All automated checks passed.")

    # Manual tests (always shown — these require real SMS/voice)
    _w()
    heading("Manual Tests (requires real modem + SIM)")
    if gsm:
        info("1. Modem status:",
             "  sudo asterisk -rx 'module show like dongle'")
        info("   Expected:", "   chan_dongle.so listed as 'active'")
        info("")
        info("2. Send SMS via Telegram:", " /sms +7XXXXXXXXXX test")
        info("   Expected:", "   SMS delivered confirmation from bot")
        info("")
        info("3. Receive call:", f"  Call {s.sim_phone} from any phone")
        info("   Expected:", "   Telegram notification about incoming call")
    if tg:
        info("1. Bot status:", "  Send /status to your Telegram bot")
        info("   Expected:", "   Bot responds with system status")
        info("")
        info("2. Send SMS:", "  /sms +7XXXXXXXXXX test")
        info("   Expected:", "   Bot confirms SMS sent via GSM node")

# ══════════════════════════════════════════════════════════════════════════════
# Summary + Handoff
# ══════════════════════════════════════════════════════════════════════════════

def phase_summary() -> None:
    heading("Summary")
    for label, val in [
        ("Role", s.node_role), ("ID", s.node_id), ("Type", s.install_type),
        ("Version", s.src_version),
        ("OS", f"{s.os_id} {s.os_ver}"), ("Config", CONF_FILE),
        ("Secrets", ENV_FILE), ("ACL", ACL_FILE), ("App", INSTALL_DIR),
        ("Venv", VENV_DIR), ("Data", DATA_DIR), ("Logs", LOG_DIR)]:
        info(label + ":", f" {val}")

    gsm = s.node_role in ("gsm", "all-in-one")
    tg = s.node_role in ("telegram", "all-in-one")
    if gsm:
        info("Services:", " simbridge-agent, simbridge-sweep.timer")
    if tg:
        info("Services:", " simbridge-userbot")

    if s.install_type == "distributed" and s.node_role == "gsm":
        _w(); info("Next: install the Telegram node.")
        info("", f"  Agent token: {s.agent_token}")

    _handoff()

def _handoff() -> None:
    HANDOFF_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    f = HANDOFF_DIR / f"install-{ts}-{s.node_id}.md"
    f.write_text("\n".join([
        "# Handoff — SimBridge Installation", "",
        f"- **Date:** {datetime.now(timezone.utc).isoformat()}",
        f"- **Node ID:** {s.node_id}",
        f"- **Role:** {s.node_role}", f"- **Type:** {s.install_type}",
        f"- **OS:** {s.os_id} {s.os_ver}",
        f"- **Version:** {s.src_version}", ""
        "## Configuration", "", f"- Config: {CONF_FILE}",
        f"- Secrets: {ENV_FILE}", f"- ACL: {ACL_FILE}", "",
        "## Services", "",
        *(["- simbridge-agent (8090)"] if s.node_role in ("gsm", "all-in-one") else []),
        *(["- simbridge-userbot"] if s.node_role in ("telegram", "all-in-one") else []),
        "", "## Details", "",
        *(f"- SIM: {s.sim_phone}" if s.sim_phone else ""),
        *(f"- Modem: {_short_modem(s.modem_model)}" if s.modem_model else ""),
        *(f"- Tailscale: {s.ts_ip}" if s.ts_ip else ""),
        *(f"- Peer: {s.peer_ip}" if s.peer_ip and s.install_type == "distributed" else ""),
        "", "## Action Required", "",
        "- [ ] Verify: `systemctl status simbridge-agent simbridge-userbot`",
        "- [ ] Test SMS and calls",
        "- [ ] Logs: `journalctl -u simbridge-agent -u simbridge-userbot`",
        ""]))
    ok("Handoff:", str(f))

# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _load_existing_config() -> None:
    """Load existing configuration from simbridge.yaml and env file.

    Populates s.* fields so phase_gather can offer them as defaults
    during update-in-place installations.
    """
    # ── Parse simbridge.yaml (simple key: value parser, no PyYAML needed) ──
    cfg_path = Path(CONF_FILE)
    if not cfg_path.exists():
        return

    yaml: Dict[str, str] = {}
    _section = ""
    try:
        for raw_line in cfg_path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            # Top-level section header (no indentation, ends with ':')
            if raw_line[0].isspace() == False and line.endswith(":"):
                _section = line[:-1]
                continue
            # List item: `- "value"`
            if line.startswith("- "):
                val = line[2:].strip().strip('"')
                if _section:
                    yaml[f"{_section}.allowed_peers"] = val
                continue
            # Key: value
            if ":" in line:
                key, _, val = line.partition(":")
                k = key.strip()
                v = val.strip().strip('"')
                if _section and "." not in k:
                    yaml[f"{_section}.{k}"] = v
                else:
                    yaml[k] = v
    except (OSError, IndexError):
        return

    # ── Parse env file (KEY=VALUE secrets) ──
    env: Dict[str, str] = {}
    env_path = Path(ENV_FILE)
    if env_path.exists():
        try:
            for raw_line in env_path.read_text().splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
        except OSError:
            pass

    # ── Parse ACL file (extract Telegram user IDs) ──
    acl_path = Path(ACL_FILE)
    if acl_path.exists():
        try:
            ids = []
            for raw_line in acl_path.read_text().splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if parts and parts[0].isdigit():
                    ids.append(parts[0])
            if ids:
                s.acl_ids = " ".join(ids)
                info("Loaded existing ACL:", f" {s.acl_ids}")
        except OSError:
            pass

    # ── Populate state from config ──
    if "node.id" in yaml and not s.node_id:
        s.node_id = yaml["node.id"]
        info("Loaded existing node_id:", s.node_id)

    if "asterisk.dongle" in yaml and s.dongle_name == "gsm":
        s.dongle_name = yaml["asterisk.dongle"]
        info("Loaded existing dongle_name:", s.dongle_name)

    if "telegram.master_username" in yaml and not s.tg_username:
        s.tg_username = yaml["telegram.master_username"]
        info("Loaded existing tg_username:", s.tg_username)

    if "sim.phone" in yaml and not s.sim_phone:
        s.sim_phone = yaml["sim.phone"]
        info("Loaded existing sim_phone:", s.sim_phone)

    if "sim.modem_model" in yaml and not s.modem_model:
        s.modem_model = yaml["sim.modem_model"]
        info("Loaded existing modem_model:", s.modem_model)

    # ── Secrets from env ──
    if "SIMBRIDGE_AGENT_TOKEN" in env and not s.agent_token:
        s.agent_token = env["SIMBRIDGE_AGENT_TOKEN"]
        info("Loaded existing agent token.")

    if "SIMBRIDGE_TG_API_ID" in env and not s.tg_api_id:
        s.tg_api_id = env["SIMBRIDGE_TG_API_ID"]
        s.tg_api_hash = env.get("SIMBRIDGE_TG_API_HASH", "")
        info("Loaded existing Telegram API credentials.")

    if "SIMBRIDGE_HTTP_SECRET" in env and not s.http_secret:
        s.http_secret = env["SIMBRIDGE_HTTP_SECRET"]
        info("Loaded existing HTTP secret.")

    if "SIMBRIDGE_AMI_PASSWORD" in env and not s.ami_pw:
        s.ami_pw = env["SIMBRIDGE_AMI_PASSWORD"]
        info("Loaded existing AMI password.")

    # ── Network (agent.listen, allowed_peers) ──
    agent_listen = yaml.get("agent.listen", "")
    if agent_listen and ":" in agent_listen and not s.own_ip:
        s.own_ip = agent_listen.rsplit(":", 1)[0]

    userbot_listen = yaml.get("userbot_http.listen", "")
    if userbot_listen and ":" in userbot_listen and not s.own_ip:
        s.own_ip = userbot_listen.rsplit(":", 1)[0]

    # Peer IP — try bridge_host first, then allowed_peers
    if not s.peer_ip:
        for src in ("voice.bridge_host", "agent.allowed_peers",
                     "userbot_http.allowed_peers"):
            val = yaml.get(src, "")
            if val and val != "127.0.0.1":
                s.peer_ip = val
                break

    if s.own_ip and s.own_ip != "127.0.0.1":
        info("Loaded existing network:", f" own={s.own_ip}, peer={s.peer_ip}")


def _write_default_asterisk_conf() -> None:
    """Create a minimal asterisk.conf when dnf/apt drops the file."""
    p = Path("/etc/asterisk/asterisk.conf")
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "; Asterisk configuration file\n"
        "\n"
        "[files]\n"
        "#astconfig = asterisk.conf\n"
        "ast_functions = functions.conf\n"
        "\n"
        "[cli]\n"
        ";highlight = yes\n"
        "update = yes\n"
        "\n"
        "[console]\n"
        "priority = -1\n"
        "\n"
        "[logging]\n"
        "\n"
        "[netsock2]\n"
        ";\n"
        "; This section defines some parameters for the network\n"
        "; listening socket (manager, IAX2, SIP, etc.)\n"
        ";\n"
        "#maxopensock = 4096\n"
        "\n"
        "[languages]\n"
        ";defaultlanguage=en\n"
        ";language=en,de,fr,it,ja,es\n"
    )

def _rand(n: int) -> str:
    return secrets.token_hex(n // 2)

def _read_ami_pw(p: Path) -> str:
    """Read the first 'secret' value from any section in manager.conf."""
    try:
        sec = False
        for ln in p.read_text().splitlines():
            s = ln.strip()
            if s.startswith("[") and s.endswith("]"):
                sec = True
                continue
            if sec and s.startswith("secret"):
                val = s.partition("=")[2].strip()
                if val:
                    return val
    except OSError:
        pass
    return ""


def _ensure_ami() -> str:
    """Resolve the AMI password — read-only, never writes Asterisk config.

    Precedence: /etc/simbridge/env (loaded into s.ami_pw on updates) ->
    manager_custom.conf (current location, written by _setup_ami) ->
    manager.conf (legacy inline section) -> generate fresh.
    _setup_ami() is responsible for actually writing manager_custom.conf.
    """
    if s.ami_pw:
        ok("AMI password:", " from /etc/simbridge/env")
        return s.ami_pw

    for p in (Path(f"{AST_DIR}/manager_custom.conf"),
              Path(f"{AST_DIR}/manager.conf")):
        pw = _read_ami_pw(p)
        if pw:
            ok("AMI password:", f" found in {p.name}")
            return pw

    pw = _rand(24)
    warn("No AMI password found — generated one (saved to /etc/simbridge/env).")
    warn("AMI password:", pw)
    return pw

# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    if os.getuid() != 0:
        fail("Run as root:", " sudo python3 install.py"); sys.exit(1)

    if sys.stderr.isatty():
        _w(f"\n{C.B}{'=' * 58}{C._0}")
        _w(f"{C.B}  SimBridge Interactive Installer{C._0}")
        _w(f"{C.B}  Telegram <-> GSM Telephony{C._0}")
        _w(f"{C.B}{'=' * 58}{C._0}\n")

    phase_type()
    phase_existing()
    phase_diag()
    phase_gather()
    phase_remove()
    phase_install()
    phase_telegram()
    phase_start()
    phase_verify()
    phase_summary()

    ok("", "Done!")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _w("\n"); warn("Aborted."); sys.exit(130)
    except Exception as e:
        _w("\n"); fail(f"Error: {e}"); sys.exit(1)
