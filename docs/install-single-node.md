# Single-Node Install

All services (Asterisk, agent, userbot) on one machine.

## Prerequisites

- AlmaLinux 9 or Ubuntu 22.04/24.04
- USB GSM modem (Huawei E173 tested) + SIM card
- Internet access (443/tcp outbound)
- Telegram user account with API credentials ([my.telegram.org](https://my.telegram.org/apps))

## Install

```bash
curl -L https://raw.githubusercontent.com/alexolvin/SimBridge/main/deploy/install.py -o install.py
sudo python3 install.py
```

Choose `Single-node (all-in-one)` when prompted.

## What the Installer Does

1. Detects OS, Python, Asterisk, chan\_dongle, Tailscale, USB modems
2. Installs system packages (python3, git, asterisk, tailscale)
3. Clones the SimBridge repository
4. Creates a Python venv and installs dependencies
5. Creates `/etc/simbridge/simbridge.yaml` with your configuration
6. Creates `/etc/simbridge/env` with secrets
7. Sets up Asterisk AMI (`/etc/asterisk/manager.conf`)
8. Installs systemd units (`simbridge-agent`, `simbridge-userbot`)
9. Configures ACL and blacklist
10. Logs in to Telegram (creates session file)
11. Starts services and runs health checks

## Post-Install

```bash
# Verify
systemctl status simbridge-agent simbridge-userbot

# Telegram — send /status
# SMS — /sms +79991234567 Hello

# Logs
journalctl -u simbridge-agent -u simbridge-userbot -f
```

## Re-running the Installer

The installer detects existing installations and offers to update or remove:

```bash
sudo python3 install.py
```

Choose `Update in place` to refresh the code and configuration, or `Remove existing and start fresh` to reinstall from scratch.

## chan_dongle

chan_dongle is **NOT** in standard repos. Install manually:

- **AlmaLinux:** see [wiringSoft.com](https://wiringSoft.com/) for RPMs
- **Ubuntu:**
  ```bash
  sudo add-apt-repository ppa:dongle-project/ppa
  sudo apt install chan-dongle
  ```

Verify: `asterisk -rx "module show like dongle"`
