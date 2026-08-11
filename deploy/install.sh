#!/bin/bash
set -euo pipefail

# SimBridge installer — AlmaLinux 8/9, Ubuntu 22.04/24.04
# Usage: ./install.sh <role> [config_path]
#   role: all-in-one | gsm | telegram

ROLE="${1:-}"
CONFIG_PATH="${2:-/etc/simbridge/simbridge.yaml}"

if [ -z "$ROLE" ]; then
    echo "Usage: $0 <all-in-one|gsm|telegram> [config_path]" >&2
    exit 1
fi

case "$ROLE" in
    all-in-one|gsm|telegram) ;;
    *) echo "Invalid role: $ROLE" >&2; exit 1 ;;
esac

echo "=== SimBridge $ROLE installer ==="

# --- Detect platform ---
if [ -f /etc/os-release ]; then
    . /etc/os-release
    ID=${ID:-}
    VERSION_ID=${VERSION_ID:-}
fi

case "$ID" in
    almalinux|rhel|centos)
        PKG_CMD="dnf install -y"
        PKG_MANAGER="dnf"
        ;;
    ubuntu)
        PKG_CMD="apt-get install -y"
        PKG_MANAGER="apt-get"
        ;;
    *)
        echo "Unsupported OS: $ID" >&2
        exit 1
        ;;
esac

echo "Platform: $ID $VERSION_ID"

# --- Detect group for service user ---
if getent group nogroup >/dev/null 2>&1; then
    SVC_GROUP="nogroup"
else
    SVC_GROUP="simbridge"
fi

# --- Dependencies ---
echo "--- Installing dependencies ---"
$PKG_MANAGER update

COMMON_DEPS="python3 python3-pip python3-venv git curl"

if [ "$PKG_MANAGER" = "dnf" ]; then
    $PKG_CMD $COMMON_DEPS
else
    $PKG_CMD $COMMON_DEPS
fi

if [ "$ROLE" = "gsm" ] || [ "$ROLE" = "all-in-one" ]; then
    echo "Installing Asterisk..."
    if [ "$PKG_MANAGER" = "dnf" ]; then
        $PKG_CMD asterisk
        # chan_dongle: install from dongle-project.repo or prebuilt RPM
        echo "WARNING: chan_dongle must be installed separately on RPM systems"
        echo "  See: https://wiringSoft.com/ for RPM packages"
    else
        $PKG_CMD asterisk
        echo "WARNING: chan_dongle must be installed from dongle-project PPA"
        echo "  sudo add-apt-repository ppa:dongle-project/ppa"
    fi
fi

# --- Create user ---
echo "--- Creating simbridge user ---"
id simbridge &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin simbridge

# Ensure the group exists (for systems without nogroup)
if ! getent group "$SVC_GROUP" >/dev/null 2>&1; then
    groupadd --system "$SVC_GROUP"
fi

# --- Create directories ---
echo "--- Creating directories ---"
mkdir -p /etc/simbridge
mkdir -p /var/lib/simbridge
mkdir -p /var/log/simbridge

# --- Copy files ---
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Config (only if not already present)
if [ ! -f "$CONFIG_PATH" ]; then
    cp "$PROJECT_ROOT/config/simbridge.example.yaml" "$CONFIG_PATH"
    echo "Config copied to $CONFIG_PATH — EDIT BEFORE STARTING"
fi

# ACL file (only if not already present)
if [ ! -f /etc/simbridge/acl.conf ]; then
    echo "# SimBridge ACL — format: <telegram_user_id> <right1> <right2> ..." > /etc/simbridge/acl.conf
    echo "# Rights: in_sms in_call out_sms out_call"
    echo "# Example: 1234567 out_sms out_call"
    echo "ACL file created at /etc/simbridge/acl.conf — ADD USERS BEFORE STARTING"
fi

# Blacklist (only if not already present)
if [ ! -f /etc/simbridge/blacklist.txt ]; then
    cp "$PROJECT_ROOT/config/blacklist.example.txt" /etc/simbridge/blacklist.txt
fi

# --- AMI configuration (for GSM role) ---
if [ "$ROLE" = "gsm" ] || [ "$ROLE" = "all-in-one" ]; then
    echo "--- Setting up AMI access ---"
    if [ ! -f /etc/asterisk/manager_custom.conf ]; then
        cat > /etc/asterisk/manager_custom.conf <<'AMIEOF'
; SimBridge AMI access — for the agent HTTP API
[pen]
include => manager_custom.conf
AMIEOF
        echo "AMI include created."
    fi
fi

# --- Python venv ---
VENV_DIR="/opt/simbridge-venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "--- Creating Python venv ---"
    python3 -m venv "$VENV_DIR"
fi

# Determine requirements based on role
if [ "$ROLE" = "gsm" ] || [ "$ROLE" = "all-in-one" ]; then
    "$VENV_DIR/bin/pip" install -r "$PROJECT_ROOT/agent/requirements.txt"
fi

if [ "$ROLE" = "telegram" ] || [ "$ROLE" = "all-in-one" ]; then
    "$VENV_DIR/bin/pip" install -r "$PROJECT_ROOT/userbot/requirements.txt"
fi

# --- Copy application code ---
echo "--- Deploying application ---"
APP_DIR="/opt/simbridge"
mkdir -p "$APP_DIR"
cp -r "$PROJECT_ROOT"/{agent,userbot,core,bridge} "$APP_DIR/"

# --- Systemd units ---
echo "--- Installing systemd units ---"
cp "$PROJECT_ROOT/deploy/systemd/simbridge-agent.service" /etc/systemd/system/
cp "$PROJECT_ROOT/deploy/systemd/simbridge-userbot.service" /etc/systemd/system/

# --- Pre-commit hook (if in git repo) ---
if [ -d "$PROJECT_ROOT/.git" ]; then
    echo "--- Installing pre-commit hook ---"
    cp "$PROJECT_ROOT/scripts/pre-commit.sh" "$PROJECT_ROOT/.git/hooks/pre-commit"
    chmod +x "$PROJECT_ROOT/.git/hooks/pre-commit"
    echo "Pre-commit hook installed."
fi

# --- Set permissions ---
echo "--- Setting permissions ---"
chown -R simbridge:"$SVC_GROUP" /opt/simbridge
chown -R simbridge:"$SVC_GROUP" /opt/simbridge-venv
chown simbridge:"$SVC_GROUP" /etc/simbridge
chmod 0640 /etc/simbridge/simbridge.yaml
chmod 0640 /etc/simbridge/acl.conf
chmod 0600 /etc/simbridge/blacklist.txt
chown simbridge:"$SVC_GROUP" /var/log/simbridge
chmod 0750 /var/log/simbridge

# Session file (will be created by the userbot)
if [ -f /var/lib/simbridge/sim_session.session ]; then
    chown simbridge:"$SVC_GROUP" /var/lib/simbridge/sim_session.session
    chmod 0600 /var/lib/simbridge/sim_session.session
fi

# --- Reload systemd ---
systemctl daemon-reload

# --- Enable services ---
echo "--- Enabling services ---"
if [ "$ROLE" = "all-in-one" ]; then
    systemctl enable --now simbridge-agent
    systemctl enable --now simbridge-userbot
elif [ "$ROLE" = "gsm" ]; then
    systemctl enable --now simbridge-agent
elif [ "$ROLE" = "telegram" ]; then
    systemctl enable --now simbridge-userbot
fi

echo ""
echo "=== Installation complete ==="
echo "  Config:     $CONFIG_PATH  (EDIT BEFORE USE)"
echo "  ACL:        /etc/simbridge/acl.conf  (ADD USERS)"
echo "  Agent:      systemctl status simbridge-agent"
echo "  Userbot:    systemctl status simbridge-userbot"
echo ""
echo "NEXT STEPS:"
echo "  1. Edit $CONFIG_PATH — set real IPs, usernames, timeouts"
echo "  2. Add users to /etc/simbridge/acl.conf"
echo "  3. Set environment secrets in systemd override:"
echo "     systemctl edit simbridge-agent"
echo "     [Service]"
echo "     Environment=\"SIMBRIDGE_AGENT_TOKEN=<your-token>\""
echo "  4. Restart: systemctl restart simbridge-agent simbridge-userbot"
