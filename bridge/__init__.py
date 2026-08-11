"""SimBridge voice media bridge — Telegram ↔ SIP.

Stage 04 implementation. Placeholder for ntgcalls/tg2sip integration.

The bridge runs on the Telegram node, next to the userbot.
Telegram traffic (MTProto + WebRTC voice) terminates here.
Only SIP + RTP crosses the tailnet to the GSM node.
"""
