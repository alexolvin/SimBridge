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

from core.config import load_config
from userbot.userbot import Userbot

logger = getLogger("simbridge.userbot.main")


async def async_main() -> None:
    basicConfig(
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        level=os.environ.get("SIMBRIDGE_LOG_LEVEL", "INFO"),
    )

    cfg_path = os.environ.get("SIMBRIDGE_CONFIG", "/etc/simbridge/simbridge.yaml")
    cfg = load_config(cfg_path)

    # Only start userbot if role is 'telegram' or 'all-in-one'
    role = cfg["node.role"]
    if role not in ("telegram", "all-in-one"):
        logger.info("Role is %s — skipping userbot startup", role)
        return

    ub = Userbot(cfg)
    await ub.start()

    # Run HTTP server for Asterisk events in parallel
    # TODO: start http_server alongside the Telethon client
    # For now, the HTTP server is started as a separate service

    await ub.run_until_disconnected()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
