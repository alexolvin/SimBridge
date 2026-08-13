
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
    _r = run_q("cd /home/user/myhub/SimBridge 2>/dev/null && git describe --tags --always 2>/dev/null")
    __version__ = _r.stdout.strip() if _r.returncode == 0 else "0.0.0+unknown"

def _read_ver(dir_: str) -> str:
    """Read VERSION from a project directory."""
    v = Path(dir_) / "VERSION"
    return v.read_text().strip() if v.exists() else "unknown"

def _ver_tuple(v: str) -> tuple:
    """Parse '0.6.0' into (0, 6, 0) for comparison."""
    parts = v.split("+")[0].split(".")  # strip metadata
    return tuple(int(p) for p in parts if p.isdigit())

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
    _w(f"{C.C}  {label}{C._0}{value}")

def warn(label: str, value: str = "") -> None:
    _w(f"{C.Y}  {label}{C._0}{value}")

def ok(label: str, value: str = "") -> None:
    _w(f"{C.G}  {label}{C._0}{value}")

def fail(label: str, value: str = "") -> None:
    _w(f"{C.R}  {label}{C._0}{value}")

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
    return {True: ("y", "yes"), False: ("n", "no")}[default].__contains__(ch) or default

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

def run(cmd: str, **kw: Any) -> subprocess.CompletedProcess[str]:
    _w(f"    $ {cmd}")
    return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          check=True, **kw)

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

    info(f"OS:", f" {s.os_id} {s.os_ver}")
    info(f"Packages:", f" {s.mgr}")

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
        if s.usb_modems:
            ok("Modem(s):")
            for m in s.usb_modems:
                info("", m.strip())

# ══════════════════════════════════════════════════════════════════════════════
# Phase 4 — Gather configuration
# ══════════════════════════════════════════════════════════════════════════════

def phase_gather() -> None:
    heading("4 / 8 — Configuration")
    s.node_id = ask("Node ID", default=os.uname().nodename)

    gsm = s.node_role in ("gsm", "all-in-one")
    tg = s.node_role in ("telegram", "all-in-one")

    if gsm:
        info("", "--- GSM / Modem ---")
        s.modem_model = ask("Modem model (e.g. Huawei E173)", required=True)
        s.sim_phone = ask("SIM phone number (e.g. +79991234567)")
        s.dongle_name = ask("chan_dongle device name", default="gsm")

        ami = Path("/etc/asterisk/manager.conf")
        if not ami.exists():
            pw = ask("AMI password (leave empty for auto-generated)", required=False)
            if not pw:
                pw = _rand(24)
                warn(f"Auto-generated AMI password: {pw}")
            s.ami_pw = pw
        else:
            cur = _read_ami_pw(ami)
            if cur:
                info("Current AMI password: set")
                pw = ask("New AMI password (empty = keep)", required=False)
                s.ami_pw = pw or cur
            else:
                s.ami_pw = ask("AMI password")

    if tg:
        info("", "--- Telegram ---")
        s.tg_api_id = ask("Telegram API_ID (my.telegram.org/apps)")
        s.tg_api_hash = ask("Telegram API_HASH")
        s.tg_username = ask("Telegram username (without @)")

    info("", "--- Secrets ---")
    if s.node_role in ("gsm", "all-in-one"):
        tok = ask("Agent token (empty = auto)", required=False)
        if not tok:
            tok = _rand(32)
            warn(f"Agent token: {tok}")
            warn("(Save — needed on Telegram node.)")
        s.agent_token = tok

    if tg:
        if s.node_role == "telegram":
            s.agent_token = ask("Agent token (must match GSM node)")
        sec = ask("HTTP secret (empty = auto)", required=False)
        if not sec:
            sec = _rand(32)
            warn(f"HTTP secret: {sec}")
        s.http_secret = sec

    if s.install_type == "distributed":
        info("", "--- Network ---")
        s.own_ip = s.ts_ip if s.ts_ip else ask("This node's Tailscale IP")
        if s.node_role == "gsm":
            s.peer_ip = ask("Telegram node Tailscale IP")
        else:
            s.peer_ip = ask("GSM node Tailscale IP")
    else:
        s.own_ip = "127.0.0.1"
        s.peer_ip = "127.0.0.1"

    info("", "--- ACL ---")
    s.acl_ids = ask("Telegram user ID(s) (space-separated)", required=False)

# ══════════════════════════════════════════════════════════════════════════════
# Phase 5 — Remove / Install
# ══════════════════════════════════════════════════════════════════════════════

def phase_remove() -> None:
    if s.action != "remove":
        return
    heading("5a / 8 — Remove Existing")
    for svc in ("simbridge-agent", "simbridge-userbot"):
        if run_ok(f"systemctl is-active --quiet {svc}"):
            info(f"Stopping {svc}...")
            run_q(f"systemctl stop {svc}"); run_q(f"systemctl disable {svc}")
    for u in ("simbridge-agent.service", "simbridge-userbot.service"):
        p = Path(f"/etc/systemd/system/{u}")
        if p.exists():
            p.unlink(); info("Removed:", f" {u}")
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
    run(f"{s.mgr} update")
    run(s.pkg.format("python3 python3-pip python3-venv git curl"))
    if gsm:
        run(s.pkg.format("asterisk"))
        run_ok(s.pkg.format("asterisk-addons"))

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
        run(f"{s.mgr} update")
    run(s.pkg.format("tailscale"))
    run_ok("systemctl enable --now tailscaled")

def _clone_repo() -> None:
    """Clone the SimBridge repo into a staging directory.

    The clone lives at /tmp/simbridge-clone-XXXX so we don't pollute
    the production path until everything is verified.
    """
    heading("Cloning SimBridge Repository")
    s.src_dir = tempfile.mkdtemp(prefix="simbridge-clone-", suffix=str(os.getpid()))
    info(f"Staging:", f" {s.src_dir}")

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
    for sub in ("agent", "userbot", "core", "bridge", "config", "deploy"):
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
    ami = Path("/etc/asterisk/manager.conf")
    if not ami.exists():
        ami.parent.mkdir(parents=True, exist_ok=True)
        ami.write_text(
            f"[general]\nenabled = yes\nport = 5038\nbindaddr = 127.0.0.1\n\n"
            f"[simbridge]\nsecret = {s.ami_pw}\n"
            f"read = system,call,log,verbose,command,agent,user\n"
            f"write = system,call,log,verbose,command,agent,user\n")
        ami.chmod(0o640)
        ok("AMI configured.")
    else:
        info(f"{ami} exists — keeping current config.")

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
  prompt: /var/lib/asterisk/sounds/custom/vm-prompt.ulaw
  ami_host: 127.0.0.1
  ami_port: 5038
  ami_username: simbridge
  ami_password_env: SIMBRIDGE_AMI_PASSWORD
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
    if not Path(BLACKLIST_FILE).exists():
        ex = Path(f"{s.src_dir}/config/blacklist.example.txt")
        if ex.exists():
            shutil.copy(ex, BLACKLIST_FILE)
        else:
            Path(BLACKLIST_FILE).write_text("# Blocked numbers (E.164, one/line)\n")
        Path(BLACKLIST_FILE).chmod(0o600)

    # Secrets
    env = ["# SimBridge secrets — NEVER commit", "",
           f"SIMBRIDGE_AGENT_TOKEN={s.agent_token}"]
    if s.tg_api_id:
        env += [f"SIMBRIDGE_TG_API_ID={s.tg_api_id}",
                f"SIMBRIDGE_TG_API_HASH={s.tg_api_hash}"]
    if s.http_secret:
        env.append(f"SIMBRIDGE_HTTP_SECRET={s.http_secret}")
    if s.ami_pw:
        env.append(f"SIMBRIDGE_AMI_PASSWORD={s.ami_pw}")
    Path(ENV_FILE).write_text("\n".join(env) + "\n"); Path(ENV_FILE).chmod(0o600)
    ok("Config:", CONF_FILE)
    ok("Secrets:", ENV_FILE)

def _install_systemd() -> None:
    heading("Installing systemd Units")
    sd = Path(f"{s.src_dir}/deploy/systemd")
    if not sd.is_dir():
        sd = Path(f"{INSTALL_DIR}/deploy/systemd")
    gsm = s.node_role in ("gsm", "all-in-one")
    tg = s.node_role in ("telegram", "all-in-one")

    if gsm and (sd / "simbridge-agent.service").exists():
        shutil.copy(sd / "simbridge-agent.service", "/etc/systemd/system/")
        ok("simbridge-agent.service")
    if tg and (sd / "simbridge-userbot.service").exists():
        shutil.copy(sd / "simbridge-userbot.service", "/etc/systemd/system/")
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
    _chown(CONF_DIR); _chown(DATA_DIR); _chown(LOG_DIR)
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
        run_ok("systemctl enable simbridge-userbot")
        info("Enabled but NOT started — after Telegram login.")
    elif gsm:
        run_ok("systemctl enable simbridge-agent")
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
            env=env, timeout=300)
    except subprocess.TimeoutExpired:
        warn("Telegram login timed out.")
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

    if gsm:
        if run_ok("systemctl is-active --quiet asterisk"):
            ok("Asterisk running.")
        else:
            warn("Asterisk not running — trying start...")
            if not run_ok("systemctl start asterisk"):
                warn("Start Asterisk manually.")
        if not run_ok("systemctl start simbridge-agent"):
            warn("Agent start deferred.")

    if tg:
        if Path(f"{DATA_DIR}/sim_session.session").exists():
            if not run_ok("systemctl start simbridge-userbot"):
                warn("Userbot start deferred.")
        else:
            warn("Telegram session not found.")

# ══════════════════════════════════════════════════════════════════════════════
# Phase 8 — Verify + Test
# ══════════════════════════════════════════════════════════════════════════════

def phase_verify() -> None:
    heading("8 / 8 — Verification")
    gsm = s.node_role in ("gsm", "all-in-one")
    tg = s.node_role in ("telegram", "all-in-one")

    if gsm:
        r = run_q("systemctl status simbridge-agent --no-pager 2>&1 | head -15")
        _w(r.stdout[:500])
        url = "http://127.0.0.1:8090/v1/health" if s.install_type == "single" \
              else f"http://{s.own_ip}:8090/v1/health"
        r = run_q(f'curl -sf {url} -H "Authorization: Bearer {s.agent_token}"')
        ok("Health:", " OK") if not r.returncode else warn("Health check failed.")
        if s.has_dongle:
            r = run_q("asterisk -rx 'dongle show status' 2>/dev/null")
            _w(r.stdout[:500])

    if tg:
        r = run_q("systemctl status simbridge-userbot --no-pager 2>&1 | head -15")
        _w(r.stdout[:500])

    # ── Test guidance ──
    heading("Testing")
    if gsm:
        info("1. Modem:", " asterisk -rx 'dongle show status'")
        info("2. SMS:", " /sms <phone> <message>")
        info("3. Call:", f" call {s.sim_phone}")
    if tg:
        info("1. Telegram — /status")
        info("2. SMS: /sms <phone> <msg>")
    if s.install_type == "distributed" and s.node_role == "telegram":
        info("Connectivity:",
             f" curl http://{s.peer_ip}:8090/v1/health "
             f"-H 'Authorization: Bearer {s.agent_token}'")

    if ask_yn("Tests completed?", False):
        if ask_yn("All passed?"):
            ok("SimBridge is operational.")
        else:
            issue = ask("Describe issues", required=False)
            if issue:
                warn("Noted:", f" {issue}")
            warn("Logs:", " journalctl -u simbridge-agent -u simbridge-userbot")
    else:
        info("Test at your convenience.")
        info("Logs:", " journalctl -u simbridge-agent -f / -u simbridge-userbot -f")

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
        info("Services:", " simbridge-agent")
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
        f"# Handoff — SimBridge Installation", "",
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
        *(f"- Modem: {s.modem_model}" if s.modem_model else ""),
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

def _rand(n: int) -> str:
    return secrets.token_hex(n // 2)

def _read_ami_pw(p: Path) -> str:
    try:
        sec = False
        for ln in p.read_text().splitlines():
            s = ln.strip()
            if s == "[simbridge]":
                sec = True; continue
            if sec and s.startswith("["):
                break
            if sec and s.startswith("secret"):
                return s.partition("=")[2].strip()
    except OSError:
        pass
    return ""

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
