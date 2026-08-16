#!/usr/bin/env python3
"""SimBridge — PC-side deployment orchestrator.

Run on your own PC (Python 3.9+, standard library only). Deploys
SimBridge onto one or two tailnet nodes by driving the on-node engine
(`deploy/install.py`, non-interactive mode) over SSH:

    tailscale ssh user@node     preferred (PC has the `tailscale` CLI)
    ssh user@node               fallback (tailnet hostname must resolve)

Per node the orchestrator:
  1. preflights: reachability, passwordless sudo, tailscale join state,
     existing-installation detection (asks "wipe or update?");
  2. bootstraps: uploads THIS repo's install.py (base64 over ssh stdin —
     no remote curl/wget dependency, version-locked to your checkout);
  3. generates the answers file locally and uploads it (chmod 0600);
  4. runs  `sudo env SIMBRIDGE_BRANCH=<br> python3 install.py
           --answers ... --result ...`  (streamed to the PC console);
  5. fetches the 0600 JSON result (base64 over ssh) and parses it.

Shared secrets (agent_token / bridge_secret / http_secret) are
pre-generated ONCE on the PC and written into every node's answers
file — both nodes get identical values, which removes the
"which node generates the token" chicken-and-egg of a two-node
deploy. The AMI password stays node-local (absent from the answers
file, the installer auto-detects/generates it per node).

Telegram login is NEVER done here (it is TTY-bound): after deploy,
log in interactively with `install.py --tg-login` — the final report
prints the exact command.

Design note (Rule 1): the orchestrator duplicates NO on-node logic.
The only new mechanism is the post-deploy cross-node health check,
which the installer cannot do: on the first pass each node is
installed with `peer_ready=false` because its peer is not up yet.
The PC performs the check from the tailnet after both nodes are up.

Prerequisites:
  - distributed deploys require the nodes to be ALREADY JOINED to the
    tailnet (one-time interactive `sudo tailscale up` beforehand);
  - the ssh user has passwordless sudo on each node.

Exit codes (mirror install.py): 0 = all ok, 1 = usage/fatal error,
2 = one or more nodes failed or the cross-node check failed.
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Remote paths (stage dir — wiped by the installer's own lifecycle) ──
STAGE_DIR      = "/var/tmp/simbridge-install"
INSTALLER_PATH = f"{STAGE_DIR}/install.py"
ANSWERS_PATH   = f"{STAGE_DIR}/answers.env"
RESULT_PATH    = f"{STAGE_DIR}/result.json"

# Ports are the fixed deployment ports (same literals install.py's
# cross-node check uses; the system ships no other listeners).
AGENT_PORT     = 8090    # /v1/health (Bearer)
USERBOT_PORT   = 8088    # /health (operational, unauthenticated)

TIMEOUT_PREFLIGHT = 30
TIMEOUT_UPLOAD    = 120
TIMEOUT_RESULT    = 60

# install.py option strings (must match deploy/install.py exactly).
OPT_SINGLE     = "Single-node (all-in-one)"
OPT_DISTRIBUTED = "Two-node (distributed)"
OPT_ROLE_GSM   = "GSM node (Asterisk + modem)"
OPT_ROLE_TG    = "Telegram node (userbot)"
OPT_WIPE       = "Remove existing and start fresh"
OPT_UPDATE     = "Update in place"


# ══════════════════════════════════════════════════════════════════════════════
# Model
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Node:
    name: str                     # tailscale hostname
    ssh_user: str
    role: str = ""                # gsm | telegram | all-in-one
    node_id: str = ""
    own_ip: str = ""
    peer_ip: str = ""
    action: str = ""              # "" | "wipe" | "update"
    ts_present: bool = False      # tailscale binary on node
    ts_joined: bool = False       # has a tailnet IP
    existing: bool = False        # prior installation detected
    ok: bool = False
    exit_code: int = -1
    result: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)


@dataclass
class Shared:
    install_type: str = "single"  # single | distributed
    branch: str = "main"
    tg_api_id: str = ""
    tg_api_hash: str = ""
    tg_username: str = ""
    acl_ids: str = ""
    sim_phone: str = ""
    modem_model: str = ""
    dongle_name: str = "gsm"
    agent_token: str = ""
    bridge_secret: str = ""
    http_secret: str = ""


# ══════════════════════════════════════════════════════════════════════════════
# Transport
# ══════════════════════════════════════════════════════════════════════════════

def detect_transport() -> str:
    """'tailscale' if the PC has the tailscale CLI, else 'ssh'."""
    return "tailscale" if shutil.which("tailscale") else "ssh"


def ssh_cmd(node: Node, transport: str) -> List[str]:
    target = f"{node.ssh_user}@{node.name}"
    if transport == "tailscale":
        return ["tailscale", "ssh", target]
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target]


def run_remote(node: Node, transport: str, cmd: str,
               timeout: Optional[int] = None, stream: bool = False,
               stdin: Optional[bytes] = None) -> Tuple[int, str]:
    """Run a remote shell command.

    stream=True echoes output line-by-line to the PC console (used for
    the long install run); otherwise output is captured silently.
    Returns (returncode, combined_output).
    """
    argv = ssh_cmd(node, transport) + [cmd]
    try:
        if stream:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            lines: List[str] = []
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.append(line)
                print(f"  [{node.name}] {line}", end="")
            rc = proc.wait()
            return rc, "".join(lines)
        p = subprocess.run(argv, input=stdin, capture_output=True,
                           text=False, timeout=timeout)
        out = (p.stdout or b"") + (p.stderr or b"")
        return p.returncode, out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except (OSError, subprocess.SubprocessError) as e:
        return 125, str(e)


def upload_remote(node: Node, transport: str, data: bytes, remote_path: str,
                  mode: str = "0600",
                  timeout: int = TIMEOUT_UPLOAD) -> Tuple[int, str]:
    """Upload bytes to the node: base64 over ssh stdin → sudo tee."""
    cmd = (f"base64 -d | sudo tee {remote_path} >/dev/null"
           f" && sudo chmod {mode} {remote_path} && echo UPLOAD_OK")
    rc, out = run_remote(node, transport, cmd, timeout=timeout, stdin=data)
    return rc, out


def pc_tailscale_ips() -> Dict[str, str]:
    """Best-effort tailnet peer IPs as seen from the PC ({} on failure)."""
    if not shutil.which("tailscale"):
        return {}
    try:
        p = subprocess.run(["tailscale", "status", "--json"],
                           capture_output=True, text=True, timeout=15)
        if p.returncode != 0:
            return {}
        d = json.loads(p.stdout)
        return {n: info.get("TailscaleIP", "")
                for n, info in d.get("Peers", {}).items()
                if info.get("Online")}
    except (subprocess.SubprocessError, ValueError, OSError):
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# Answers file
# ══════════════════════════════════════════════════════════════════════════════

def answers_env_text(d: Dict[str, str]) -> str:
    """Serialize answers as key=value lines (install.py --answers format).

    Values with spaces are quoted; install.py strips one matching pair
    of surrounding quotes.
    """
    lines = ["# SimBridge answers file — generated by install_remote.py",
             "# Consumed by install.py --answers (see its docstring)."]
    for k, v in d.items():
        v = str(v).strip()
        lines.append(f'{k} = "{v}"' if " " in v else f"{k} = {v}")
    return "\n".join(lines) + "\n"


def build_answers(node: Node, sh: Shared) -> Dict[str, str]:
    """Answers dict for one node. Key set mirrors install.py phase_gather."""
    d: Dict[str, str] = {}
    d["install_type"] = OPT_SINGLE if sh.install_type == "single" \
        else OPT_DISTRIBUTED
    if sh.install_type == "distributed":
        d["node_role"] = OPT_ROLE_GSM if node.role == "gsm" else OPT_ROLE_TG
    if node.action:
        d["action"] = OPT_WIPE if node.action == "wipe" else OPT_UPDATE
    # Node may lack tailscale entirely; the installer installs it when
    # absent (no-op package install when already present).
    d["install_tailscale"] = "true" if not node.ts_present else "false"
    d["install_tailscale_opt"] = "false"
    d["node_id"] = node.node_id
    if node.role in ("gsm", "all-in-one"):
        d["modem_model"] = sh.modem_model
        d["sim_phone"] = sh.sim_phone
        d["dongle_name"] = sh.dongle_name
    if node.role in ("telegram", "all-in-one"):
        d["tg_api_id"] = sh.tg_api_id
        d["tg_api_hash"] = sh.tg_api_hash
        d["tg_username"] = sh.tg_username
    # Pre-generated on the PC — identical on every node.
    d["agent_token"] = sh.agent_token
    d["bridge_secret"] = sh.bridge_secret
    d["http_secret"] = sh.http_secret
    if sh.install_type == "distributed":
        d["own_ip"] = node.own_ip
        d["peer_ip"] = node.peer_ip
    d["acl_ids"] = sh.acl_ids
    # Login is TTY-bound (later, via --tg-login); the peer is not up
    # during the first pass (the PC cross-checks afterwards).
    d["tg_login"] = "false"
    d["peer_ready"] = "false"
    return d


# ══════════════════════════════════════════════════════════════════════════════
# Preflight
# ══════════════════════════════════════════════════════════════════════════════

PREFLIGHT_CMD = (
    "echo USER=$(id -un); "
    "echo TS_IP=$(tailscale ip -4 2>/dev/null | head -1); "
    "if systemctl list-unit-files 2>/dev/null | grep -q '^simbridge-'"
    " || [ -d /etc/simbridge ] || [ -d /opt/simbridge ]; "
    "then echo EXISTING=1; else echo EXISTING=0; fi; "
    "echo SUDO_OK=$(sudo -n true 2>/dev/null && echo 1 || echo 0); "
    "echo TS_PRESENT=$(command -v tailscale >/dev/null 2>&1 && echo 1"
    " || echo 0)"
)


def parse_preflight(out: str) -> Dict[str, str]:
    d: Dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            d[k.strip()] = v.strip()
    return d


def preflight(node: Node, transport: str,
              attempts: int = 2) -> Tuple[bool, str]:
    """Reachability + state probe. Returns (ok, detail)."""
    last = ""
    for i in range(attempts):
        rc, out = run_remote(node, transport, PREFLIGHT_CMD,
                             timeout=TIMEOUT_PREFLIGHT)
        last = out or f"(rc={rc})"
        if rc != 0:
            continue
        d = parse_preflight(out)
        if "SUDO_OK" not in d:
            continue
        node.own_ip = d.get("TS_IP", "")
        node.ts_joined = bool(node.own_ip)
        node.ts_present = d.get("TS_PRESENT") == "1"
        node.existing = d.get("EXISTING") == "1"
        if d.get("SUDO_OK") != "1":
            return False, "passwordless sudo not available for the ssh user"
        return True, f"ssh user '{d.get('USER', '?')}'"
    return False, f"unreachable (last output: {last.strip()[:200]})"


def resolve_ips(nodes: List[Node], sh: Shared) -> List[str]:
    """Fill in own/peer IPs for a distributed deploy.

    Own IP: node's own tailnet IP (preflight) → PC's tailscale view →
    user. Peer IP: the other node's own IP → PC view → user.
    Returns a list of human-readable problems (empty = all resolved).
    """
    if sh.install_type != "distributed":
        return []
    problems: List[str] = []
    pc_ips = pc_tailscale_ips()
    for n in nodes:
        if not n.own_ip:
            n.own_ip = pc_ips.get(n.name, "")
        if not n.own_ip:
            n.own_ip = q(f"No tailnet IP for {n.name} — enter it manually"
                         f" (or leave empty to block the node)", "")
        if not n.own_ip:
            problems.append(
                f"node {n.name}: no tailnet IP — join the tailnet first"
                f" (one-time interactive: sudo tailscale up) or provide"
                f" the IP manually")
    if len(nodes) == 2:
        for n in nodes:
            other = next(o for o in nodes if o is not n)
            n.peer_ip = other.own_ip
            if not n.peer_ip:
                problems.append(f"{n.name}: peer IP unknown"
                                f" ({other.name} has none)")
    return problems


# ══════════════════════════════════════════════════════════════════════════════
# Per-node install
# ══════════════════════════════════════════════════════════════════════════════

def install_node(node: Node, transport: str, sh: Shared,
                 install_py: Path) -> bool:
    """Bootstrap + answers + install + result for one node."""
    print(f"\n── Deploying node {node.name} ({node.role}, {node.node_id})")

    # 1. Bootstrap the installer itself from this checkout.
    data = install_py.read_bytes()
    rc, out = run_remote(node, transport, f"sudo mkdir -p {STAGE_DIR}",
                         timeout=TIMEOUT_PREFLIGHT)
    if rc != 0:
        node.errors.append(f"mkdir {STAGE_DIR}: {out.strip()[:200]}")
        return False
    rc, out = upload_remote(node, transport, data, INSTALLER_PATH, mode="0750")
    if "UPLOAD_OK" not in out:
        node.errors.append(f"upload install.py failed: {out.strip()[:200]}")
        return False
    rc, out = run_remote(node, transport,
                         f"sudo python3 -m py_compile {INSTALLER_PATH}"
                         f" && echo BOOTSTRAP_OK",
                         timeout=TIMEOUT_PREFLIGHT)
    if "BOOTSTRAP_OK" not in out:
        node.errors.append(f"bootstrap py_compile failed: {out.strip()[:200]}")
        return False

    # 2. Answers file.
    answers = answers_env_text(build_answers(node, sh))
    rc, out = upload_remote(node, transport, answers.encode(), ANSWERS_PATH)
    if "UPLOAD_OK" not in out:
        node.errors.append(f"upload answers failed: {out.strip()[:200]}")
        return False

    # 3. Run the installer (streamed — this takes minutes).
    cmd = (f"sudo env SIMBRIDGE_BRANCH={sh.branch} python3 {INSTALLER_PATH}"
           f" --answers {ANSWERS_PATH} --result {RESULT_PATH}")
    print(f"  [{node.name}] $ {cmd}")
    rc_install, out = run_remote(node, transport, cmd, stream=True)
    node.exit_code = rc_install
    if rc_install == 1:
        node.errors.append("installer failed (exit 1) — see log above")
        return False
    if rc_install not in (0, 2):
        node.errors.append(f"installer exited {rc_install} — see log above")
        return False

    # 4. Fetch the result JSON (0600 root file → base64 over ssh).
    rc, out = run_remote(node, transport, f"sudo base64 {RESULT_PATH}",
                         timeout=TIMEOUT_RESULT)
    if rc == 0 and out.strip():
        try:
            node.result = json.loads(
                base64.b64decode(out.strip()).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            node.errors.append("could not parse result JSON")
    else:
        node.errors.append("could not fetch result JSON (continuing on"
                           " installer exit code)")

    # 5. Clean the stage secrets (keep install.py for --tg-login reuse).
    run_remote(node, transport,
               f"sudo rm -f {ANSWERS_PATH} {RESULT_PATH}",
               timeout=TIMEOUT_RESULT)

    node.ok = (rc_install == 0)
    if rc_install == 2:
        failed = node.result.get("verify", {}).get("failed", [])
        node.errors.append("installed, but verify failed: "
                           + ", ".join(failed))
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Post-deploy cross-node check (PC-side, tailnet-local)
# ══════════════════════════════════════════════════════════════════════════════

def check_url(url: str, token: Optional[str] = None) -> Tuple[bool, str]:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200, r.read(200).decode("utf-8", "replace")
    except Exception as e:                      # noqa: BLE001 — report any
        return False, str(e)


def cross_check(nodes: List[Node], sh: Shared,
                transport: str) -> List[str]:
    """Operational health of both endpoints. Returns problem list."""
    problems: List[str] = []
    if sh.install_type == "single":
        n = nodes[0]
        for label, cmd in (
            ("userbot /health",
             f"curl -sf -m 10 http://127.0.0.1:{USERBOT_PORT}/health"),
            ("agent /v1/health",
             f"curl -sf -m 10 -H 'Authorization: Bearer {sh.agent_token}'"
             f" http://127.0.0.1:{AGENT_PORT}/v1/health"),
        ):
            rc, out = run_remote(n, transport, cmd, timeout=30)
            if rc != 0:
                problems.append(f"{n.name}: {label} unreachable")
        return problems
    gsm = next(n for n in nodes if n.role == "gsm")
    tg = next(n for n in nodes if n.role == "telegram")
    for label, url, tok in (
        ("TG node /health", f"http://{tg.own_ip}:{USERBOT_PORT}/health", None),
        ("GSM agent /v1/health",
         f"http://{gsm.own_ip}:{AGENT_PORT}/v1/health", sh.agent_token),
    ):
        good, detail = check_url(url, tok)
        if not good:
            problems.append(f"{label} ({url}) failed: {detail[:120]}")
    return problems


# ══════════════════════════════════════════════════════════════════════════════
# PC-side questions
# ══════════════════════════════════════════════════════════════════════════════

def q(label: str, default: str = "", required: bool = False) -> str:
    """PC-side input; Enter accepts the default. EOF (piped run) → default."""
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            v = input(f"{label}{suffix}: ").strip()
        except EOFError:
            return default
        if v or default:
            return v or default
        if not required:
            return ""
        print("  (required)")


def gather_params() -> Tuple[List[Node], Shared]:
    sh = Shared()
    print("SimBridge remote installer — PC side")
    print("The on-node engine is install.py (non-interactive).")

    t = q("Deployment: [s]ingle node / [t]wo nodes", "s")
    sh.install_type = "distributed" if t.lower().startswith("t") else "single"

    nodes: List[Node] = []
    n = 2 if sh.install_type == "distributed" else 1
    for i in range(1, n + 1):
        name = q(f"Node {i} tailscale name", required=True)
        user = q(f"Node {i} ssh user", required=True)
        nodes.append(Node(name=name, ssh_user=user))

    if sh.install_type == "distributed":
        which = q("Which node is the GSM node? (1 or 2)", "1")
        gsm_idx = 1 if which.strip() == "2" else 0
        nodes[gsm_idx].role = "gsm"
        nodes[1 - gsm_idx].role = "telegram"
        for node, default in zip(nodes, ("gsm-1", "tg-1")):
            node.node_id = q(f"Node {node.name} id", default)
    else:
        nodes[0].role = "all-in-one"
        nodes[0].node_id = q("Node id", "simbridge-1")

    needs_gsm = any(x.role in ("gsm", "all-in-one") for x in nodes)
    needs_tg = any(x.role in ("telegram", "all-in-one") for x in nodes)
    if needs_gsm:
        sh.sim_phone = q("SIM phone number (E.164)", required=True)
        sh.modem_model = q("Modem model (lsusb string)", required=True)
        sh.dongle_name = q("chan_dongle device name", "gsm")
    if needs_tg:
        sh.tg_api_id = q("Telegram API_ID (my.telegram.org/apps)", required=True)
        sh.tg_api_hash = q("Telegram API_HASH", required=True)
        sh.tg_username = q("Master Telegram username (without @)",
                           required=True)
    sh.acl_ids = q("ACL Telegram user IDs (comma-separated)", required=True)
    sh.branch = q("Git branch to deploy", "main")
    return nodes, sh


# ══════════════════════════════════════════════════════════════════════════════
# Orchestration
# ══════════════════════════════════════════════════════════════════════════════

def orchestrate(args: argparse.Namespace) -> int:
    transport = detect_transport()
    if transport == "ssh":
        print("note: no `tailscale` CLI on this PC — using plain ssh"
              " (tailnet hostnames must resolve)")
    nodes, sh = gather_params()

    # Shared secrets: one pair of values for the whole deployment.
    sh.agent_token = secrets.token_hex(16)
    sh.bridge_secret = secrets.token_hex(16)
    sh.http_secret = secrets.token_hex(16)

    install_py = Path(__file__).resolve().parent / "install.py"
    if not install_py.exists():
        print(f"ERROR: {install_py} not found — run from the repo checkout")
        return 1

    if args.dry_run:
        for node in nodes:
            print(f"\n── DRY RUN — node {node.name} ({node.role}) ──")
            print(answers_env_text(build_answers(node, sh)))
            print(f"  $ sudo env SIMBRIDGE_BRANCH={sh.branch} python3"
                  f" {INSTALLER_PATH} --answers {ANSWERS_PATH}"
                  f" --result {RESULT_PATH}")
        print("\nDry run — nothing was executed on any node.")
        return 0

    # Preflight every node.
    for node in nodes:
        print(f"\n── Preflight {node.name} ({node.ssh_user}@{node.name})")
        ok, detail = preflight(node, transport)
        if not ok:
            node.errors.append(f"preflight: {detail}")
            continue
        if node.ts_joined:
            ts_state = f"joined {node.own_ip}"
        elif node.ts_present:
            ts_state = "present, not joined"
        else:
            ts_state = "absent"
        print(f"  ok — {detail}, tailscale {ts_state}")
        if node.existing:
            a = q(f"  {node.name} has an existing installation —"
                  f" [w]ipe it / [u]pdate in place", "u")
            node.action = "wipe" if a.lower().startswith("w") else "update"

    # IPs (distributed only).
    problems = resolve_ips(nodes, sh)
    for p in problems:
        print(f"  ! {p}")
    blocked = [nd for nd in nodes
               if nd.errors
               or (sh.install_type == "distributed" and not nd.own_ip)]

    deployed: List[Node] = []
    for node in nodes:
        if node in blocked:
            continue
        if install_node(node, transport, sh, install_py):
            deployed.append(node)
            print(f"  node {node.name}: "
                  f"{'OK' if node.ok else 'installed, verify issues'}"
                  + (f" (v{node.result.get('version', '?')})"
                     if node.result else ""))
        else:
            print(f"  node {node.name}: FAILED")

    # Cross-node health — only when every node deployed.
    final_problems: List[str] = []
    if len(deployed) == len(nodes) and nodes:
        final_problems = cross_check(deployed, sh, transport)
    elif len(deployed) == 0:
        final_problems = ["no node deployed"]

    # Report.
    print("\n" + "═" * 64)
    print("DEPLOYMENT REPORT")
    print("═" * 64)
    for node in nodes:
        status = "OK" if node.ok else ("FAILED" if not node.result
                                       else "installed, verify issues")
        print(f"  {node.name:20s} {node.role:10s} {status}"
              + (f"  v{node.result.get('version')}" if node.result else ""))
        for e in node.errors:
            print(f"      ! {e}")
    for p in final_problems:
        print(f"  ! cross-check: {p}")
    if not final_problems and nodes:
        print("  cross-check: both endpoints healthy")
    tg = next((x for x in nodes if x.role in ("telegram", "all-in-one")
               and x.ok), None)
    if tg:
        print(f"\nNext step — Telegram login (interactive, one time):")
        print(f"  tailscale ssh {tg.ssh_user}@{tg.name}")
        print(f"  sudo python3 /opt/simbridge/deploy/install.py --tg-login")
    print()
    return 0 if not final_problems and all(x.ok for x in nodes) else 2


def main() -> None:
    ap = argparse.ArgumentParser(
        description="SimBridge PC-side deployment orchestrator")
    ap.add_argument("--dry-run", action="store_true",
                    help="ask the questions, print the answers files and"
                         " commands, touch no node")
    args = ap.parse_args()
    sys.exit(orchestrate(args))


if __name__ == "__main__":
    main()
