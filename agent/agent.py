"""SimBridge agent — main FastAPI application.

Runs on the GSM node. Binds to the Tailscale interface only.
Replaces the SSH+shell-interpolation path for outgoing SMS.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from logging import getLogger
from typing import AsyncGenerator

from fastapi import FastAPI

from core.config import load_config, redact_config
from core.audit import AuditLogger
from core.contacts import ContactResolver
from core.blacklist import BlacklistManager
from core.sms_correlation import SMSCorrelationStore
from core.call_control import CallRegistry
from core.acl import ACLManager
from core.modem import ModemPool, SingleModemProvider
from agent.routes import router as api_router
from agent.deps import init_deps
from agent.ami_client import AMIClient

logger = getLogger("simbridge.agent")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup / shutdown lifecycle."""
    config_path = os.environ.get("SIMBRIDGE_CONFIG", "/etc/simbridge/simbridge.yaml")
    cfg = load_config(config_path)

    # Log effective config with secrets redacted
    redacted = redact_config(cfg)
    logger.info("Agent started with config: %s", redacted)

    # Store config on app.state for dependency injection
    app.state.cfg = cfg

    # Initialize audit logger
    audit = AuditLogger(cfg["paths.audit_log"])
    app.state.audit = audit

    # Initialize AMI client — values from config, not hardcoded
    ami_host = cfg.get("asterisk.ami_host", "127.0.0.1")
    ami_port = cfg.get("asterisk.ami_port", 5038)
    ami_username = cfg.get("asterisk.ami_username", "simbridge")

    ami_password_env = cfg.get("asterisk.ami_password_env", "SIMBRIDGE_AMI_PASSWORD")
    ami_password = os.environ.get(ami_password_env, "")

    ami = AMIClient(
        host=ami_host,
        port=ami_port,
        username=ami_username,
        password=ami_password,
        dongle=cfg["asterisk.dongle"],
    )
    app.state.ami = ami

    try:
        await ami.connect()
        logger.info("AMI connected to Asterisk at %s:%s", ami_host, ami_port)
    except ConnectionError as e:
        logger.error(
            "AMI connection failed: %s — SMS will fail until Asterisk is reachable", e
        )

    # Initialize rate limiters
    from core.ratelimit import RateLimiter

    app.state.sms_limiter = RateLimiter(
        max_requests=cfg["limits.sms_per_hour"],
        window_seconds=3600,
    )

    # Initialize contact resolver (S02.1)
    contacts = ContactResolver(csv_path=cfg["paths.contacts_cache"])
    app.state.contacts = contacts

    # Initialize blacklist manager (S02.2)
    blacklist = BlacklistManager(path=cfg["paths.blacklist"])
    app.state.blacklist = blacklist

    # Initialize SMS correlation store (S02.3)
    sms_store = SMSCorrelationStore()
    app.state.sms_store = sms_store

    # Initialize modem pool (S05.1)
    modem_provider = SingleModemProvider(
        modem_id=cfg.get("asterisk.dongle", "gsm"),
        device=cfg.get("asterisk.dongle", "gsm"),
    )
    modem_pool = ModemPool(provider=modem_provider)
    app.state.modem_pool = modem_pool

    # Initialize call registry (S04.2)
    call_registry = CallRegistry(
        sms_store=sms_store,
        audit=audit,
        modem_pool=modem_pool,
    )
    app.state.call_registry = call_registry

    # Initialize ACL manager (S04.3)
    acl = ACLManager(path=cfg["telegram.acl_file"])
    app.state.acl = acl

    # Initialize auth/replay state from config
    init_deps(app)

    yield

    # Shutdown
    await ami.close()
    logger.info("Agent shut down")


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title="SimBridge Agent",
        version="0.1.0",
        lifespan=lifespan,
    )

    # API routes (auth is applied at router level via router.dependencies)
    app.include_router(api_router, prefix="/v1")

    return app


app = create_app()
