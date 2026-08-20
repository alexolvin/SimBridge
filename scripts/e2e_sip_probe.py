#!/usr/bin/env python3
"""E2E SIP probe — two-phase verification of the PJSIP leg without GSM
side effects (S04.2 verification, Rule 3: real-device evidence).

Run this FROM the Telegram node (or any host with tailnet reach to the
GSM node). It plays the role of the sip-tg-bridge UAC, twice:

Phase A (media, --media-exten, default 778):
  anonymous INVITE -> 401 Digest challenge -> authenticated INVITE
  -> 200 OK + media SDP -> ACK -> ulaw RTP both ways -> BYE from
  Asterisk. Extension 778 plays 5 s of silence and hangs up on its
  own; the silence playback is what makes Asterisk actually emit RTP,
  so the rtp_rx criterion proves the RETURN media path over the
  tailnet (comedia, non-NAT peer). No GSM leg, no agent.

Phase B (nocal, --exten, default 777):
  the same call cycle against the production-shaped pattern
  extension. The dialplan answers, asks the agent about the call
  (unknown call -> 404 -> CALL_ID empty) and hangs up at the "nocal"
  branch — the GSM leg is NEVER dialed. That path writes NO audio
  frames to the channel, so Asterisk sends zero RTP: rtp_rx is NOT a
  pass criterion here and the received count is reported
  informationally. Criteria: 401, 200 OK + SDP, BYE.

PASS criteria (all must hold in the phase that expects them):
  - 401 challenge seen            (inbound SIP auth enforced)
  - 200 OK with media SDP         (transport bound, endpoint live)
  - incoming RTP packets received (Phase A only: return media path)
  - BYE from Asterisk             (dialplan executed, clean hangup,
                                    no orphan channel, no GSM dial)

Each phase starts a fresh dialog (new Call-ID and a fresh anonymous
INVITE, so each phase gets its own Digest nonce).

Exit code 0 = all criteria met, 1 = at least one failed.

Usage (on the TG node):
  . /etc/simbridge/env
  python3 e2e_sip_probe.py --gsm-ip 100.124.155.106 \
      --secret "$SIMBRIDGE_BRIDGE_SECRET"

The advertised source IP (Via/Contact/SDP o=/c=) is taken from
--local-ip, else $SIMBRIDGE_NODE_TAILSCALE_IP, else the source
address the kernel selects for packets toward --gsm-ip. It must NOT
be 0.0.0.0: for a non-NAT peer Asterisk sends RTP to the SDP c=
address, so 0.0.0.0 would black-hole the media.

Dialplan note (Asterisk 18.26.4, main/pbx.c): with
extenpatternmatchnew=0 (the default; no config option, runtime CLI
only) the extension walker returns the FIRST matching extension in
FILE order. Hence 778 is defined BEFORE the _X. pattern in the
[tg-bridge] context — otherwise an INVITE to 778 would match _X.
(the production nocal path) and never reach the media target. The
X pattern digit class is [0-9] (case 'X' in _extension_match_core),
so 11-digit production numbers (79xx...) also match _X. and the
probe extension is unreachable from real traffic: 778 matches only
the literal string "778".
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import socket
import struct
import sys
import time


def detect_local_ip(dest_ip: str, dest_port: int = 5060) -> str:
    """Source address the kernel will use for packets to dest (UDP
    connect() sets the default route without sending anything)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((dest_ip, dest_port))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Probe:
    def __init__(self, gsm_ip: str, secret: str, exten: str,
                 media_exten: str, timeout: float,
                 local_ip: str = "") -> None:
        self.gsm_ip = gsm_ip
        self.secret = secret
        self.exten = exten            # phase B target (nocal, no media)
        self.b_exten = exten          # stable copy: _reset_call() overwrites
        self.media_exten = media_exten  # phase A target (silence playback)
        self.timeout = timeout

        # SIP socket (port 0 -> OS picks)
        self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip.bind(("", 0))
        self.sip.settimeout(0.5)
        self.local_port = self.sip.getsockname()[1]
        # A socket bound to "" reports "0.0.0.0" from getsockname(), but the
        # kernel sources packets from the route's outgoing address. Advertise
        # THAT real IP in Via/Contact/SDP: for a non-NAT peer Asterisk sends
        # RTP to the SDP c= address, so 0.0.0.0 would black-hole the media.
        self.local_ip = (local_ip
                         or os.environ.get("SIMBRIDGE_NODE_TAILSCALE_IP", "")
                         or detect_local_ip(gsm_ip))

        # RTP socket on a random high port
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind(("", 0))
        self.rtp.settimeout(0.5)
        self.rtp_port = self.rtp.getsockname()[1]

        self.phase_results: list[tuple[str, dict[str, bool], int]] = []
        self._phase = "?"
        self._reset_call(self.exten)

    # ── per-call (per-phase) state ────────────────────────────────────

    def _reset_call(self, exten: str) -> None:
        """Start a fresh dialog: new Call-ID/From-tag, fresh CSeq,
        empty buffers. The next anonymous INVITE fetches a fresh
        Digest nonce from the 401 challenge."""
        self.exten = exten
        self.uri = f"sip:{exten}@{self.gsm_ip}"
        self.call_id = f"e2e-{secrets.token_hex(8)}@{self.local_ip}"
        self.from_tag = secrets.token_hex(8)
        self.to_tag = ""
        self.cseq = 0
        self.last_branch = ""
        self.rtp_rx = 0
        self.sip_buf = b""
        self.peer_rtp: tuple[str, int] | None = None

    # ── SIP message assembly ──────────────────────────────────────────

    def _headers(self, method: str, uri: str, extra: str = "") -> str:
        self.cseq += 1
        if method == "INVITE":
            self.last_branch = f"z9hG4bK{secrets.token_hex(16)}"
        to = f"<sip:{self.exten}@{self.gsm_ip}>"
        if self.to_tag:
            to += f";tag={self.to_tag}"
        return (
            f"{method} {uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port}"
            f";branch={self.last_branch}\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <sip:e2e@simbridge>;tag={self.from_tag}\r\n"
            f"To: {to}\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self.cseq} {method}\r\n"
            f"Contact: <sip:e2e@{self.local_ip}:{self.local_port}>\r\n"
            f"{extra}"
        )

    def _send_invite(self, auth: str = "") -> None:
        sdp = (
            f"v=0\r\no=e2e 1 1 IN IP4 {self.local_ip}\r\n"
            "s=simbridge-e2e\r\n"
            f"c=IN IP4 {self.local_ip}\r\nt=0 0\r\n"
            f"m=audio {self.rtp_port} RTP/AVP 0\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
        )
        extra = f"Authorization: Digest {auth}\r\n" if auth else ""
        head = self._headers("INVITE", self.uri,
                             extra + "Content-Type: application/sdp\r\n")
        if auth:
            print(f"--- phase {self._phase} INVITE (authenticated) as sent ---")
            print(head)
        self.sip.sendto((head + f"Content-Length: {len(sdp)}\r\n\r\n"
                         + sdp).encode(), (self.gsm_ip, 5060))

    def _send_ack(self) -> None:
        # RFC 3261: ACK reuses the INVITE's CSeq number (and branch).
        self.cseq -= 1
        head = self._headers("ACK", self.uri)
        self.sip.sendto((head + "Content-Length: 0\r\n\r\n").encode(),
                        (self.gsm_ip, 5060))

    def _send_bye_ok(self, bye: str) -> None:
        """Answer an incoming BYE with a 200 OK echoing its dialog."""
        call = re.search(r"Call-ID: (.*)", bye).group(1).strip()
        frm = re.search(r"^From: (.*)$", bye, re.M).group(1).strip()
        to = re.search(r"^To: (.*)$", bye, re.M).group(1).strip()
        cseq = re.search(r"^CSeq: (.*)$", bye, re.M).group(1).strip()
        if f";tag=" not in to:
            to += f";tag={secrets.token_hex(8)}"
        ok = (
            "SIP/2.0 200 OK\r\n"
            f"Via: {re.search(r'^Via: (.*)$', bye, re.M).group(1).strip()}\r\n"
            f"From: {frm}\r\nTo: {to}\r\n"
            f"Call-ID: {call}\r\nCSeq: {cseq}\r\n"
            f"Contact: <sip:e2e@{self.local_ip}:{self.local_port}>\r\n"
            "Content-Length: 0\r\n\r\n"
        )
        self.sip.sendto(ok.encode(), (self.gsm_ip, 5060))

    # ── SIP message receiving ─────────────────────────────────────────

    def _extract_sip(self) -> list[str]:
        """Pull complete SIP messages out of sip_buf.
        Content-Length is parsed leniently: PJSIP right-justifies the
        value in a 3-char field (e.g. 'Content-Length:  43')."""
        out: list[str] = []
        while b"\r\n\r\n" in self.sip_buf:
            head, _, rest = self.sip_buf.partition(b"\r\n\r\n")
            m = re.search(rb"Content-Length:\s*(\d+)", head, re.I)
            n = int(m.group(1)) if m else 0
            if len(rest) < n:
                break
            body, self.sip_buf = rest[:n], rest[n:]
            out.append((head + b"\r\n\r\n" + body).decode(
                errors="replace"))
        return out

    def _drain_sip(self, deadline: float, want: str = "") -> list[str]:
        """Recv until the socket is quiet (0.5 s) or deadline; return
        complete SIP messages. If `want` is given, return as soon as a
        message starting with it has been extracted (early ACK: the
        media window starts sooner)."""
        out: list[str] = []
        while time.time() < deadline:
            try:
                data, _ = self.sip.recvfrom(65535)
                self.sip_buf += data
            except socket.timeout:
                break
            out.extend(self._extract_sip())
            if want and any(m.startswith(want) for m in out):
                return out
        return out

    @staticmethod
    def _digest(method: str, uri: str, challenge: str,
                user: str, secret: str) -> str:
        realm = re.search(r'realm="([^"]+)"', challenge).group(1)
        nonce = re.search(r'nonce="([^"]+)"', challenge).group(1)
        qop_m = re.search(r'qop="([^"]+)"', challenge)
        qop = qop_m.group(1).split(",")[0] if qop_m else None
        ha1 = hashlib.md5(f"{user}:{realm}:{secret}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        if qop:
            # RFC 2617 §3.3.2: with qop the response covers
            # HA1:nonce:nc:cnonce:qop:HA2
            cnonce = secrets.token_hex(8)
            nc = "00000001"
            resp = hashlib.md5(
                f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
            ).hexdigest()
            return (f'Algorithm=MD5, username="{user}", realm="{realm}", '
                    f'nonce="{nonce}", uri="{uri}", qop={qop}, '
                    f'nc={nc}, cnonce="{cnonce}", response="{resp}"')
        resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return (f'Algorithm=MD5, username="{user}", realm="{realm}", '
                f'nonce="{nonce}", uri="{uri}", response="{resp}"')

    # ── one full call cycle (one phase) ───────────────────────────────

    def _run_call(self, phase: str, exten: str, expect_media: bool):
        self._phase = phase
        self._reset_call(exten)
        res = {"401_auth_challenge": False,
               "200_ok_media": False,
               "rtp_rx": False,
               "bye_from_asterisk": False}

        # 1. anonymous INVITE -> 401
        self._send_invite()
        msgs = self._drain_sip(time.time() + 5, want="SIP/2.0 401")
        m401 = next((m for m in msgs if m.startswith("SIP/2.0 401")), "")
        if not m401:
            print(f"--- phase {phase}: expected 401 challenge, got:")
            for m in msgs[:2]:
                print(m[:400])
            return res, self.rtp_rx
        res["401_auth_challenge"] = True

        # 2. authenticated INVITE -> 200 OK + SDP (early return -> fast ACK)
        chal = re.search(r"WWW-Authenticate: (.*)", m401).group(1).strip()
        self._send_invite(self._digest("INVITE", self.uri, chal,
                                       "tg-bridge", self.secret))
        msgs = self._drain_sip(time.time() + self.timeout,
                               want="SIP/2.0 200")
        m200 = next((m for m in msgs
                     if m.startswith("SIP/2.0 200") and "m=audio" in m),
                    "")
        if not m200:
            print(f"--- phase {phase}: expected 200 OK with media, got:")
            for m in msgs[:3]:
                print(m)
            return res, self.rtp_rx
        res["200_ok_media"] = True

        # parse remote media addr:port from the answer SDP
        body = m200.split("\r\n\r\n", 1)[1]
        mtgt = re.search(r"m=audio (\d+)", body)
        ctgt = re.search(r"c=IN IP4 (\S+)", body)
        self.peer_rtp = (ctgt.group(1), int(mtgt.group(1)))
        to_m = re.search(r"To: .*?tag=([^;\r]+)", m200)
        self.to_tag = to_m.group(1) if to_m else ""
        self._send_ack()

        # 3+4. media phase: stream ulaw silence, collect RTP, await BYE
        self._media_phase(expect_media, res)
        rtp_count = self.rtp_rx
        if not expect_media:
            # the nocal path writes no audio frames; rtp_rx is not a
            # criterion, only reported
            del res["rtp_rx"]
        return res, rtp_count

    def _media_phase(self, expect_media: bool, res: dict[str, bool]) -> None:
        assert self.peer_rtp
        deadline = time.time() + self.timeout
        seq, ssrc = 0, secrets.randbelow(2 ** 31)
        silence = b"\xff" * 160  # 20 ms of ulaw silence
        byed = False
        bye_grace = 0.0  # after BYE: brief drain for in-flight RTP
        last_send = 0.0
        # Short 20 ms socket timeouts -> a true 50 pps send loop with
        # prompt BYE detection (the old blocking 0.5 s recvfrom degraded
        # the loop to ~1 pps).
        self.sip.settimeout(0.02)
        self.rtp.settimeout(0.02)
        try:
            while time.time() < deadline:
                now = time.time()
                if now - last_send >= 0.02:
                    hdr = struct.pack("!BBHII", 0x80, 0, seq & 0xFFFF,
                                      0, ssrc)
                    self.rtp.sendto(hdr + silence, self.peer_rtp)
                    seq += 1
                    last_send = now
                try:
                    data, _ = self.rtp.recvfrom(1500)
                    # RTP v2 header: top two bits = version (10). The old
                    # check `data[0] & 0x60 == 0x80` was always False
                    # (0x60 & 0x80 == 0) and counted nothing.
                    if len(data) >= 12 and data[0] & 0xC0 == 0x80:
                        self.rtp_rx += 1
                except socket.timeout:
                    pass
                try:
                    data, _ = self.sip.recvfrom(65535)
                    self.sip_buf += data
                    for m in self._extract_sip():
                        if m.startswith("BYE "):
                            byed = True
                            self._send_bye_ok(m)
                except socket.timeout:
                    pass
                if byed:
                    if not bye_grace:
                        bye_grace = time.time() + 0.5
                    if time.time() >= bye_grace:
                        break
        finally:
            self.sip.settimeout(0.5)
            self.rtp.settimeout(0.5)
        res["rtp_rx"] = self.rtp_rx > 0
        res["bye_from_asterisk"] = byed

    # ── main flow ─────────────────────────────────────────────────────

    def run(self) -> bool:
        # Phase A: media target — proves the return RTP path
        res_a, rtp_a = self._run_call("A", self.media_exten,
                                      expect_media=True)
        self.phase_results.append(
            (f"A (media, exten {self.media_exten})", res_a, rtp_a))
        # let Asterisk release the channel before the next dialog
        time.sleep(1.0)
        # Phase B: production shape, nocal teardown — no media expected.
        # b_exten (not self.exten): phase A's _reset_call overwrote it.
        res_b, rtp_b = self._run_call("B", self.b_exten, expect_media=False)
        self.phase_results.append(
            (f"B (nocal, exten {self.b_exten})", res_b, rtp_b))
        return self._report()

    def _report(self) -> bool:
        print()
        print("=== E2E SIP probe results ===")
        ok = True
        for label, res, rtp_count in self.phase_results:
            print(f"  Phase {label}:")
            for name, got in res.items():
                print(f"    {'PASS' if got else 'FAIL'}: {name}")
                ok = ok and got
            if "rtp_rx" in res:
                print(f"    RTP packets received: {rtp_count}")
            else:
                print(f"    RTP packets received: {rtp_count} "
                      f"(not a criterion on the nocal path — it writes "
                      f"no audio frames)")
        print("=== " + ("ALL PASS" if ok else "FAILURES PRESENT") + " ===")
        return ok


def main() -> int:
    ap = argparse.ArgumentParser(
        description="E2E SIP probe for the SimBridge PJSIP leg (two-phase)")
    ap.add_argument("--gsm-ip", required=True,
                    help="GSM node tailnet IP (Asterisk SIP target)")
    ap.add_argument("--secret", required=True,
                    help="SIMBRIDGE_BRIDGE_SECRET (shared SIP credential)")
    ap.add_argument("--media-exten", default="778",
                    help="phase A target — silence playback, media path")
    ap.add_argument("--exten", default="777",
                    help="phase B target — production shape, nocal branch")
    ap.add_argument("--local-ip", default="",
                    help="advertised source IP in Via/Contact/SDP "
                         "(default: $SIMBRIDGE_NODE_TAILSCALE_IP, then "
                         "auto-detected from the route to --gsm-ip)")
    ap.add_argument("--timeout", type=float, default=20.0,
                    help="media-phase window in seconds")
    a = ap.parse_args()
    probe = Probe(a.gsm_ip, a.secret, a.exten, a.media_exten, a.timeout,
                  a.local_ip)
    return 0 if probe.run() else 1


if __name__ == "__main__":
    sys.exit(main())
