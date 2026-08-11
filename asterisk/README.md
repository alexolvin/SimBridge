# Asterisk Configuration Examples

This directory contains example Asterisk configuration files for SimBridge.
These are **templates** — copy them to `/etc/asterisk/` and adapt for your
environment.

| File | Purpose |
|---|---|
| `extensions.conf.example` | Dialplan: incoming call, SMS, voicemail, report routing |
| `pjsip.conf.example` | PJSIP endpoint for tg-bridge (Stage 04 voice bridge) |
| `dongle.conf.example` | chan_dongle modem configuration |

## Installation

```bash
sudo cp asterisk/extensions.conf.example /etc/asterisk/extensions_custom.conf
sudo cp asterisk/pjsip.conf.example /etc/asterisk/pjsip_custom.conf
sudo cp asterisk/dongle.conf.example /etc/asterisk/dongle_custom.conf
sudo asterisk -rx "core reload"
```
