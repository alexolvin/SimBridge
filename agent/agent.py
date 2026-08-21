"""SimBridge agent — main FastAPI application.

Runs on the GSM node. Binds to the Tailscale interface only.
Replaces the SSH+shell-interpolation path for outgoing SMS.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from logging import getLogger
from typing import AsyncGenerator

from fastapi import FastAPI, Request

from core.config import load_config, redact_config
from core.audit import AuditLogger
from core.contacts import ContactResolver
from core.blacklist import BlacklistManager
from core.sms_correlation import SMSCorrelationStore
from core.call_control import CallRegistry
from core.acl import ACLManager
from core.modem import ModemPool, SingleModemProvider, is_broken
from core.logging_config import setup_logging, set_correlation
from core.metrics import MetricsCollector
from core.health import HealthChecker
from core.alerting import AlertManager
from core.recovery import BackoffReconnector, ModemWatchdog
from agent.routes import router as api_router
from agent.deps import init_deps
from agent.ami_client import AMIClient
from agent.ami_reconnect import AMIReconnect
from agent.supervisor import run_supervisor

logger = getLogger("simbridge.agent")

# S06.2: how many AMI reconnect attempts before giving up (and alerting).
AMI_RECONNECT_MAX_RETRIES = 10


def modem_alert_rule(message: str) -> str:
    """Map a watchdog alert message to an alert rule name (S06.2).

    The watchdog emits human-readable messages ("{label} recovered",
    "{label} stuck — reset failed: ..."); the rule is the stable machine
    name used for cooldowns. Kept as a pure function so it is testable
    without instantiating a watchdog.
    """
    return "modem_recovery" if "recovered" in message else "dongle_offline"


def make_modem_check(provider, modem_id: str):
    """Build the watchdog health check for one modem (S06.2).

    The watchdog recovers a broken device, not a busy one: busy states
    (CALL_BUSY/SMS_BUSY/BUSY) and INITIALIZING are normal operation — a
    "reset" (AMI reconnect) mid-call would drop the active call's event
    stream, and a registering modem has not failed. is_available() must
    not be used here: it conflates "cannot take new work" with "broken".
    Before the first poll the state is the constructor default (OFFLINE),
    not a device report — not a failure either (boot grace).

    Kept as a factory (not an inline closure) so the production wiring
    is unit-testable without booting the app.
    """
    async def modem_check() -> bool:
        if not provider.has_observed(modem_id):
            return True
        return not is_broken(provider.get_info(modem_id))
    return modem_check


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup / shutdown lifecycle."""
    config_path = os.environ.get("SIMBRIDGE_CONFIG", "/etc/simbridge/simbridge.yaml")
    cfg = load_config(config_path)

    # S06.2: Setup structured JSON logging
    log_level = os.environ.get("SIMBRIDGE_LOG_LEVEL", "INFO")
    setup_logging(level=log_level, json_format=True)

    # Log effective config with secrets redacted
    redacted = redact_config(cfg)
    logger.info("Agent started with config: %s", redacted)

    # Store config on app.state for dependency injection
    app.state.cfg = cfg

    # S06.2: Initialize metrics collector
    metrics = MetricsCollector()
    app.state.metrics = metrics

    # S06.2: Initialize health checker
    # (AMI client will be set after initialization below)
    health_checker = HealthChecker(ami=None, cfg=cfg)
    app.state.health_checker = health_checker

    # S06.2: Alerting. The agent has no Telegram client of its own, so
    # alerts travel over the tailnet to the userbot node, which owns the
    # Telegram session and forwards them to the master user.
    userbot_url = (cfg.get("agent.userbot_url") or "").rstrip("/")
    userbot_secret_env = cfg.get("userbot_http.secret_env", "SIMBRIDGE_HTTP_SECRET")
    userbot_secret = os.environ.get(userbot_secret_env, "")

    async def alert_send(message: str) -> None:
        import httpx

        if not userbot_url:
            raise ConnectionError("agent.userbot_url not configured")
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.post(
                f"{userbot_url}/events/alert",
                json={"message": message},
                headers={"x-simbridge-secret": userbot_secret},
            )
            if resp.status_code >= 400:
                raise ConnectionError(
                    f"userbot /events/alert returned {resp.status_code}"
                )

    alerts = AlertManager(send_fn=alert_send)
    alerts.register_rule("ami_down", cooldown_seconds=600)
    app.state.alerts = alerts

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

    # S06.3: AMI auto-reconnect on failure. AMIClient has no internal
    # reconnect — without this, a dropped AMI link is fatal until a
    # manual restart.
    async def ami_reconnect() -> None:
        await ami.connect()

    async def on_ami_give_up() -> None:
        logger.critical(
            "AMI reconnect gave up after %d attempts — agent non-functional",
            AMI_RECONNECT_MAX_RETRIES,
        )
        await alerts.alert(
            "ami_down",
            f"AMI reconnect failed after {AMI_RECONNECT_MAX_RETRIES} "
            f"attempts — agent non-functional",
        )

    ami_reconnector = BackoffReconnector(
        operation=ami_reconnect,
        label="AMI",
        min_delay=2.0,
        max_delay=60.0,
        max_retries=AMI_RECONNECT_MAX_RETRIES,
        on_give_up=on_ami_give_up,
    )
    app.state.ami_reconnector = ami_reconnector

    # S06.3: one self-healing AMI client — any ConnectionError from any
    # caller (request, poller, health check) kicks the reconnector;
    # start() is a no-op while one is already in flight.
    app.state.ami = AMIReconnect(ami, ami_reconnector)

    try:
        await app.state.ami.connect()
        logger.info("AMI connected to Asterisk at %s:%s", ami_host, ami_port)
    except ConnectionError as e:
        logger.error(
            "AMI connection failed: %s — backoff reconnect started", e
        )

    # S06.2: Wire health checker with the (wrapped) AMI client
    health_checker._ami = app.state.ami
    app.state.health_checker = health_checker

    # Initialize rate limiters
    from core.ratelimit import RateLimiter

    app.state.sms_limiter = RateLimiter(
        max_requests=cfg["limits.sms_per_hour"],
        window_seconds=3600,
    )

    # S06.1: Call rate limiter per user (limits.calls_per_minute)
    app.state.call_limiter = RateLimiter(
        max_requests=cfg["limits.calls_per_minute"],
        window_seconds=60,
    )

    # Initialize contact resolver (S02.1)
    contacts = ContactResolver(csv_path=cfg["paths.contacts_cache"])
    app.state.contacts = contacts

    # Initialize blacklist manager (S02.2)
    blacklist = BlacklistManager(path=cfg["paths.blacklist"])
    app.state.blacklist = blacklist

    # Initialize SMS correlation store (S02.3) — persistent: delivery
    # reports survive restarts via the JSONL log.
    sms_store = SMSCorrelationStore(log_path=cfg["paths.sms_correlation"])
    app.state.sms_store = sms_store

    # Initialize modem pool (S05.1)
    dongle_id = cfg.get("asterisk.dongle", "gsm")
    modem_provider = SingleModemProvider(
        modem_id=dongle_id,
        device=dongle_id,
        sim_number=cfg.get("sim.phone") or None,
    )
    modem_pool = ModemPool(provider=modem_provider)
    app.state.modem_pool = modem_pool

    # S05.1: derive modem state from the real device — without the poller
    # the provider stays OFFLINE forever and every outgoing call 503s.
    # S06.2: the poller also feeds the modem_registered metric.
    from agent.modem_poll import run_modem_poller

    poll_interval = float(cfg.get("watchdog.modem_check_seconds", 30))
    poller_stop = asyncio.Event()
    poller_task = asyncio.create_task(
        run_modem_poller(
            app.state.ami,
            modem_provider,
            dongle_id,
            poll_interval,
            poller_stop,
            metrics=metrics,
        )
    )
    app.state.modem_poller_task = poller_task

    # Initialize call registry (S04.2)
    # S06.2: call outcome + duration metrics are recorded via the
    # registry's transition hook — one funnel, no per-route counters.
    call_registry = CallRegistry(
        sms_store=sms_store,
        audit=audit,
        modem_pool=modem_pool,
        metrics=metrics,
    )
    app.state.call_registry = call_registry

    # S06.2: modem watchdog — if the modem stays in a broken state
    # (OFFLINE/ERROR) for max_resets consecutive checks, attempt a
    # reset and alert on the outcome. The poller owns state
    # detection; the watchdog owns recovery (see agent.py honesty
    # report on the reset mechanism). Check semantics: busy is not
    # broken, unobserved is not failed (make_modem_check).
    modem_check = make_modem_check(modem_provider, dongle_id)

    async def modem_reset() -> None:
        # The only reset verified to exist in this repo: AMI reconnect.
        # A USB power-cycle would need hardware control the nodes do not
        # have (no udev rule, no AMI reset action — Rule 2).
        await ami.connect()

    async def modem_alert_fn(message: str) -> None:
        await alerts.alert(modem_alert_rule(message), message)

    modem_watchdog = ModemWatchdog(
        check_fn=modem_check,
        reset_fn=modem_reset,
        label=dongle_id,
        check_interval=poll_interval,
        max_resets=3,
        alert_fn=modem_alert_fn,
    )
    await modem_watchdog.start()
    app.state.modem_watchdog = modem_watchdog

    # S06.2: supervisor — edge-triggered alerts from the health checker
    # (dongle present / registration / peer reachability) plus
    # component-metric refresh from the same check.
    supervisor_stop = asyncio.Event()
    supervisor_task = asyncio.create_task(
        run_supervisor(
            health_checker,
            modem_provider,
            dongle_id,
            metrics,
            alerts,
            poll_interval,
            supervisor_stop,
        )
    )
    app.state.supervisor_task = supervisor_task

    # Initialize ACL manager (S04.3)
    acl = ACLManager(path=cfg["telegram.acl_file"])
    app.state.acl = acl

    # Initialize auth/replay state from config
    init_deps(app)

    yield

    # Shutdown
    supervisor_stop.set()
    await supervisor_task
    modem_watchdog.stop()
    ami_reconnector.stop()
    poller_stop.set()
    await poller_task
    await ami.close()
    logger.info("Agent shut down")


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title="SimBridge Agent",
        version="0.1.0",
        lifespan=lifespan,
    )

    # S06.2: correlation IDs on every request. The inbound
    # x-correlation-id (already used by the replay guard) is reused when
    # present, so one ID links the HTTP request, the JSON log lines and
    # the audit record; otherwise a fresh one is minted and returned to
    # the caller.
    @app.middleware("http")
    async def set_request_correlation(request: Request, call_next):
        cid = request.headers.get("x-correlation-id") or uuid.uuid4().hex
        set_correlation(cid)
        response = await call_next(request)
        response.headers["x-correlation-id"] = cid
        return response

    # API routes (auth is applied at router level via router.dependencies)
    app.include_router(api_router, prefix="/v1")

    return app


app = create_app()
