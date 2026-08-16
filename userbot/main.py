#!/usr/bin/env python3
"""Userbot entry point.

Usage::

    SIMBRIDGE_CONFIG=/etc/simbridge/simbridge.yaml \
    SIMBRIDGE_TG_API_ID=... \
    SIMBRIDGE_TG_API_HASH=... \
    python -m userbot.main
"""

from __future__ import annotations

import os
import sys
import asyncio
from logging import getLogger

from core.alerting import AlertManager
from core.config import load_config, redact_config
from core.logging_config import setup_logging
from core.metrics import MetricsCollector
from userbot.userbot import Userbot

logger = getLogger("simbridge.userbot.main")


async def async_main() -> None:
    # S06.2: structured JSON logging (same discipline as the agent),
    # one line per event, correlation IDs included.
    log_level = os.environ.get("SIMBRIDGE_LOG_LEVEL", "INFO")
    setup_logging(level=log_level, json_format=True)

    cfg_path = os.environ.get("SIMBRIDGE_CONFIG", "/etc/simbridge/simbridge.yaml")
    cfg = load_config(cfg_path)

    # Log the effective config with secrets redacted (S01.2) — same
    # discipline as agent.py.
    logger.info(
        "Userbot starting with config: %s",
        redact_config(cfg),
    )

    # Only start userbot if role is 'telegram' or 'all-in-one'
    role = cfg["node.role"]
    if role not in ("telegram", "all-in-one"):
        logger.info("Role is %s — skipping userbot startup", role)
        return

    ub = Userbot(cfg)
    await ub.start()

    # S06.2: userbot-side metrics (incoming SMS, telegram_connected),
    # exported at /health for the agent's peer check.
    metrics = MetricsCollector()

    # S06.2: local alerts go straight to Telegram — this node owns the
    # session, so no HTTP hop (the agent, which has no session, uses
    # /events/alert on this node instead).
    async def tg_alert_send(message: str) -> None:
        await ub.client.send_message(ub.master_id, message)

    alerts = AlertManager(send_fn=tg_alert_send)

    # D1/D14: run the Asterisk-event HTTP server in-process, on the
    # same event loop as the Telethon client. One systemd unit covers
    # both; the server exits with the process.
    from userbot.http_server import create_http_server

    app = create_http_server(
        secret=os.environ[cfg["userbot_http.secret_env"]],
        allowed_peers=cfg.get("userbot_http.allowed_peers", []),
        acl=ub.acl,
        audit=ub.audit,
        contacts=ub.contacts,
        client=ub.client,
        master_id=ub.master_id,
        metrics=metrics,
    )

    import uvicorn

    host, _, port = cfg["userbot_http.listen"].rpartition(":")
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=int(port)))
    server_task = asyncio.create_task(server.serve())

    # S06.2: survive Telegram session drops — alert, reconnect with
    # backoff, and only exit (for the systemd re-auth restart) when
    # the retries are exhausted.
    await ub.run_with_recovery(alerts=alerts)

    server.should_exit = True
    await server_task


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
