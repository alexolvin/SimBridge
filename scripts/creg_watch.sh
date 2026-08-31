#!/usr/bin/env bash
# creg_watch.sh — unlimited registration watcher for SimBridge (TZ-02 v2 §4).
#
# Runs as a systemd service (creg-watch.service, root). Polls the EC25
# read-only through chan_dongle inside the running Asterisk
# (`asterisk -rx 'dongle cmd gsm ...'`) — the same mechanism as the 48h
# flash-session watcher (creg_watch_48h.sh, 2026-08-30). No direct AT-port
# access, no writes to the module: AT+CREG? / AT+CEREG? / AT+QNWINFO only.
#
# Output goes to stdout and is captured by journald (Storage=persistent on
# this node). No time limit — the loop never exits on its own.
#
# Registration criterion: stat is the SECOND comma-separated field of
# "+CREG: n,stat[,lac,ci,tech]". A registered response carries 5 fields
# (e.g. "2,1,612D,3410,0"), so a trailing-field pattern misses it (and a
# cell type of 1/5 could false-positive). Trigger on stat 1 (registered)
# or 5 (registered, roaming) of CREG (GSM) or CEREG (LTE).
# REGRESSION NOTE: the trailing-field parse bug was fixed once already
# (creg_watch_48h.sh); TZ-02 v2 §4.4 — do not regress.
set -u

FULL=/var/log/asterisk/full
INTERVAL=120

# q <ATCMD> <marker> -> value after the marker in the fresh response line
# (or empty). chan_dongle wraps the response in single quotes
# ("user's command:'+CREG: 2,2'") — strip everything up to the marker
# and the trailing quote.
q() {
  local L0 R
  L0=$(wc -l < "$FULL" 2>/dev/null) || return
  asterisk -rx "dongle cmd gsm $1" >/dev/null 2>&1
  sleep 3
  R=$(tail -n +$((L0+1)) "$FULL" 2>/dev/null | grep -a "$2" | tail -1 | sed "s/^.*$2//; s/'\$//")
  [ -n "$R" ] && printf '%s' "$R"
}

echo "=== creg-watch started (pid $$) host=$(hostname) ==="
echo "PASSIVE: read-only AT via chan_dongle (CREG/CEREG/QNWINFO); interval ${INTERVAL}s; no time limit"

REG_SEEN=0
i=0
while :; do
  i=$((i+1))
  CREG=$(q 'AT+CREG?' '+CREG: ')
  CEREG=$(q 'AT+CEREG?' '+CEREG: ')
  QNW=$(q 'AT+QNWINFO' '+QNWINFO: ')
  # stat = second comma-separated field (see header).
  CREG_STAT=${CREG#*,}; CREG_STAT=${CREG_STAT%%,*}
  CEREG_STAT=${CEREG#*,}; CEREG_STAT=${CEREG_STAT%%,*}
  if [ -z "$CREG" ] && [ -z "$CEREG" ]; then
    echo "$(date '+%F %T') sample $i EMPTY (asterisk down or dongle busy) — retry next cycle"
  else
    echo "$(date '+%F %T') sample $i CREG=[$CREG] CEREG=[$CEREG] QNW=[$QNW]"
  fi
  if [ "$CREG_STAT" = "1" ] || [ "$CREG_STAT" = "5" ] \
     || [ "$CEREG_STAT" = "1" ] || [ "$CEREG_STAT" = "5" ]; then
    if [ $REG_SEEN -eq 0 ]; then
      REG_SEEN=1
      echo "$(date '+%F %T') *** REGISTERED (stat 1/5: CREG=[$CREG] CEREG=[$CEREG]) after $i samples — dongle show devices:"
      asterisk -rx 'dongle show devices' 2>&1 | sed 's/^/    /'
    fi
  else
    REG_SEEN=0
  fi
  sleep "$INTERVAL"
done
