#!/usr/bin/env python3
"""E2E SIP probe — proves the PJSIP leg end-to-end without GSM side
effects (S04.2 verification, Rule 3: real-device evidence).

Run this FROM the Telegram node (or any host with tailnet reach to the
GSM node). It plays the role of the sip-tg-bridge UAC:

  1. Anonymous INVITE           -> 401 Digest challenge (auth enforced)
  2. Authenticated INVITE       -> 100 Trying -> 200 OK + media SDP
  3. ACK + ulaw RTP, both ways  -> media path over the tailnet
  4. Expects Asterisk to BYE on its own: the [tg-bridge] dialplan
     answers, asks the agent about the call (unknown call -> 404 ->
     CALL_ID empty) and hangs up at the "nocal" branch — the GSM leg
     is NEVER dialed. The extension is a dummy (default "777", which
     the carrier rejects instantly even if nocal were ever skipped).

PASS criteria (all must hold):
  - 401 challenge seen            (inbound SIP auth enforced)
  - 200 OK with media SDP         (transport bound, endpoint live)
  - incoming RTP packets received (RTP over tailnet, comedia)
  - BYE from Asterisk             (dialplan executed, clean hangup,
                                    no orphan channel, no GSM dial)

Exit code 0 = all criteria met, 1 = at least one failed.

Usage (on the TG node):
  python3 e2e_sip_probe.py --gsm-ip 100.124.155.106 \
      --secret "$(grep ^SIMBRIDGE_BRIDGE_SECRET /etc/simbridge/env | cut -d= -f2)"
"""

from __future__ import annotations

import argparse
import hashlib
import re
import secrets
import socket
import struct
import sys
import time


class Probe:
    def __init__(self, gsm_ip: str, secret: str, exten: str,
                 timeout: float) -> None:
        self.gsm_ip = gsm_ip
        self.secret = secret
        self.exten = exten
        self.timeout = timeout
        self.uri = f"sip:{exten}@{gsm_ip}:5060;transport=udp"

        # SIP socket (port 0 -> OS picks)
        self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip.bind(("", 0))
        self.sip.settimeout(0.5)
        self.local_ip, self.local_port = self.sip.getsockname()[:2]

        # RTP socket on a random high port
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind(("", 0))
        self.rtp.settimeout(0.5)
        self.rtp_port = self.rtp.getsockname()[1]

        self.call_id = f"e2e-{secrets.token_hex(8)}@{self.local_ip}"
        self.from_tag = secrets.token_hex(8)
        self.to_tag = ""
        self.cseq = 0
        self.last_branch = ""
        self.rtp_rx = 0
        self.sip_buf = b""
        self.results: dict[str, bool] = {}
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

    def _drain_sip(self, deadline: float) -> list[str]:
        """Recv until the socket is quiet (0.5 s) or deadline; return
        complete SIP messages."""
        out: list[str] = []
        while time.time() < deadline:
            try:
                data, _ = self.sip.recvfrom(65535)
            except socket.timeout:
                break
            self.sip_buf += data
            while b"\r\n\r\n" in self.sip_buf:
                head, _, rest = self.sip_buf.partition(b"\r\n\r\n")
                m = re.search(rb"Content-Length: (\d+)", head, re.I)
                n = int(m.group(1)) if m else 0
                if len(rest) < n:
                    break
                body, self.sip_buf = rest[:n], rest[n:]
                out.append((head + b"\r\n\r\n" + body).decode(
                    errors="replace"))
        return out

    @staticmethod
    def _digest(method: str, uri: str, challenge: str,
                user: str, secret: str) -> str:
        realm = re.search(r'realm="([^"]+)"', challenge).group(1)
        nonce = re.search(r'nonce="([^"]+)"', challenge).group(1)
        ha1 = hashlib.md5(f"{user}:{realm}:{secret}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return (f'Algorithm=MD5, username="{user}", realm="{realm}", '
                f'nonce="{nonce}", uri="{uri}", response="{resp}"')

    # ── main flow ─────────────────────────────────────────────────────

    def run(self) -> bool:
        self.results = {"401_auth_challenge": False,
                        "200_ok_media": False,
                        "rtp_rx": False,
                        "bye_from_asterisk": False}

        # 1. anonymous INVITE -> 401
        self._send_invite()
        msgs = self._drain_sip(time.time() + 5)
        m401 = next((m for m in msgs if m.startswith("SIP/2.0 401")), "")
        if not m401:
            print("--- expected 401 challenge, got:")
            for m in msgs[:2]:
                print(m[:400])
            return self._report()
        self.results["401_auth_challenge"] = True

        # 2. authenticated INVITE -> 200 OK + SDP
        chal = re.search(r"WWW-Authenticate: (.*)", m401).group(1).strip()
        self._send_invite(self._digest("INVITE", self.uri, chal,
                                       "tg-bridge", self.secret))
        msgs = self._drain_sip(time.time() + self.timeout)
        m200 = next((m for m in msgs
                     if m.startswith("SIP/2.0 200") and "m=audio" in m),
                    "")
        if not m200:
            print("--- expected 200 OK with media, got:")
            for m in msgs[:3]:
                print(m[:400])
            return self._report()
        self.results["200_ok_media"] = True

        # parse remote media addr:port from the answer SDP
        body = m200.split("\r\n\r\n", 1)[1]
        mtgt = re.search(r"m=audio (\d+)", body)
        ctgt = re.search(r"c=IN IP4 (\S+)", body)
        self.peer_rtp = (ctgt.group(1), int(mtgt.group(1)))
        to_m = re.search(r"To: .*?tag=([^;\r]+)", m200)
        self.to_tag = to_m.group(1) if to_m else ""
        self._send_ack()

        # 3+4. media phase: stream ulaw silence, collect RTP, await BYE
        self._media_phase()
        return self._report()

    def _media_phase(self) -> None:
        assert self.peer_rtp
        deadline = time.time() + self.timeout
        seq, ssrc = 0, secrets.randbelow(2 ** 31)
        silence = b"\xff" * 160  # 20 ms of ulaw silence
        byed = False
        last_send = 0.0
        while time.time() < deadline and not (byed and self.rtp_rx > 0):
            now = time.time()
            if now - last_send >= 0.02:  # 50 pps
                hdr = struct.pack("!BBHII", 0x80, 0, seq & 0xFFFF, 0, ssrc)
                self.rtp.sendto(hdr + silence, self.peer_rtp)
                seq += 1
                last_send = now
            try:
                data, _ = self.rtp.recvfrom(1500)
                if len(data) >= 12 and data[0] & 0x60 == 0x80:
                    self.rtp_rx += 1
            except socket.timeout:
                pass
            for m in self._drain_sip(deadline):
                if m.startswith("BYE "):
                    byed = True
                    self._send_bye_ok(m)
        self.results["rtp_rx"] = self.rtp_rx > 0
        self.results["bye_from_asterisk"] = byed

    def _report(self) -> bool:
        print()
        print("=== E2E SIP probe results ===")
        ok = True
        for name, got in self.results.items():
            print(f"  {'PASS' if got else 'FAIL'}: {name}")
            ok = ok and got
        print(f"  incoming RTP packets: {self.rtp_rx}")
        print("=== " + ("ALL PASS" if ok else "FAILURES PRESENT") + " ===")
        return ok


def main() -> int:
    ap = argparse.ArgumentParser(
        description="E2E SIP probe for the SimBridge PJSIP leg")
    ap.add_argument("--gsm-ip", required=True,
                    help="GSM node tailnet IP (Asterisk SIP target)")
    ap.add_argument("--secret", required=True,
                    help="SIMBRIDGE_BRIDGE_SECRET (shared SIP credential)")
    ap.add_argument("--exten", default="777",
                    help="dummy extension — never dialed (nocal branch)")
    ap.add_argument("--timeout", type=float, default=20.0,
                    help="media-phase window in seconds")
    a = ap.parse_args()
    probe = Probe(a.gsm_ip, a.secret, a.exten, a.timeout)
    return 0 if probe.run() else 1


if __name__ == "__main__":
    sys.exit(main())
