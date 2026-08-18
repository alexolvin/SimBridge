#!/usr/bin/env bash
# wait-tailnet-ip — ExecStartPre guard for the Asterisk service.
#
# In distributed mode the PJSIP transport binds a specific Tailscale IP
# (see [transport-udp] in the generated pjsip.conf). At boot the tailnet
# IP may not be assigned yet; if the bind fails, res_pjsip drops the
# transport object entirely ("could not be started: Cannot assign
# requested address") and the endpoint/aor/auth objects that reference
# it are gone — no SIP until the next manual restart.
#
# This script waits (up to 90 s) for the bind address to appear on a
# local interface. Non-fatal by design: if the IP never shows up
# (tailscale down, wrong node), Asterisk still starts so that SMS and
# the dongle keep working; the SIP leg will be absent and the agent
# health endpoint will report bridge=unreachable.
set -u

IP=""
if [ -r /etc/simbridge/env ]; then
    IP=$(sed -n 's/^SIMBRIDGE_NODE_TAILSCALE_IP=//p' /etc/simbridge/env | head -1)
fi
if [ -z "$IP" ] && [ -r /etc/asterisk/pjsip.conf ]; then
    IP=$(sed -n 's/^bind=//p' /etc/asterisk/pjsip.conf | head -1)
fi
# Nothing to wait for: single-node mode binds 127.0.0.1 or no IP set.
[ -z "$IP" ] && exit 0
case "$IP" in 127.0.0.1) exit 0 ;; esac

i=0
while [ "$i" -lt 45 ]; do
    if ip -4 addr show 2>/dev/null | grep -q " inet ${IP}/"; then
        echo "wait-tailnet-ip: ${IP} is up on a local interface" >&2
        exit 0
    fi
    sleep 2
    i=$((i + 1))
done
echo "wait-tailnet-ip: ${IP} not up after 90s, starting Asterisk anyway" >&2
exit 0
