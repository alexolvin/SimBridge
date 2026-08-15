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
from logging import getLogger, basicConfig

from core.config import load_config, redact_config
from userbot.userbot import Userbot

logger = getLogger("simbridge.userbot.main")


async def async_main() -> None:
    basicConfig(
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        level=os.environ.get("SIMBRIDGE_LOG_LEVEL", "INFO"),
    )

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
    )

    import uvicorn

    host, _, port = cfg["userbot_http.listen"].rpartition(":")
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=int(port)))
    server_task = asyncio.create_task(server.serve())

    await ub.run_until_disconnected()

    server.should_exit = True
    await server_task


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
