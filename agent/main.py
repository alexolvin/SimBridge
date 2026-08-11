#!/usr/bin/env python3
"""SimBridge agent — CLI entry point.

Usage::

    SIMBRIDGE_CONFIG=/etc/simbridge/simbridge.yaml \
    SIMBRIDGE_AGENT_TOKEN=... \
    python -m agent.main
"""

from __future__ import annotations

import os
import sys

import uvicorn

from core.config import load_config


def main() -> None:
    cfg_path = os.environ.get("SIMBRIDGE_CONFIG", "/etc/simbridge/simbridge.yaml")
    cfg = load_config(cfg_path)

    role = cfg["node.role"]
    if role not in ("gsm", "all-in-one"):
        print(f"Role is {role} — skipping agent startup", file=sys.stderr)
        sys.exit(0)

    listen = cfg["agent.listen"]
    host, port = listen.rsplit(":", 1)

    uvicorn.run(
        "agent.agent:app",
        host=host,
        port=int(port),
        log_level=os.environ.get("SIMBRIDGE_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
