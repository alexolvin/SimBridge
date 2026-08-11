"""Agent dependencies: authentication, IP allowlist, replay protection, DI wiring."""

from __future__ import annotations

import time
import threading
from typing import Optional

from fastapi import Depends, HTTPException, Request

# ---------------------------------------------------------------------------
# Global state — initialized at app startup
# ---------------------------------------------------------------------------

_agent_token: Optional[str] = None
_allowed_peers: Optional[list[str]] = None
_replay_window: int = 300  # seconds
_seen_ids: dict[str, float] = {}
_replay_lock = threading.Lock()


def init_deps(app) -> None:
    """Call from lifespan to set global auth/replay state from config."""
    global _agent_token, _allowed_peers, _replay_window

    cfg = app.state.cfg
    import os

    token_env = cfg.get("agent.token_env", "SIMBRIDGE_AGENT_TOKEN")
    _agent_token = os.environ.get(token_env)
    _allowed_peers = cfg.get("agent.allowed_peers", [])
    _replay_window = cfg.get("limits.replay_window_seconds", 300)


# ---------------------------------------------------------------------------
# Auth checks — called via Depends(require_auth) on every route
# ---------------------------------------------------------------------------

async def check_auth(request: Request) -> None:
    """Verify bearer token."""
    auth_header = request.headers.get("authorization", "")
    token = auth_header.replace("Bearer ", "").strip()
    if not _agent_token:
        raise HTTPException(
            status_code=500,
            detail="Agent token not configured",
        )
    if not token or token != _agent_token:
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


async def check_ip(request: Request) -> None:
    """Verify client IP is in allowlist."""
    if not _allowed_peers:
        return  # no allowlist configured — allow all (dev mode)

    client_host = request.client.host if request.client else None
    if client_host not in _allowed_peers:
        raise HTTPException(
            status_code=403,
            detail=f"IP {client_host} not in allowed peers",
        )


async def check_replay(request: Request) -> None:
    """Reject duplicate correlation_ids within the replay window."""
    cid = request.headers.get("x-correlation-id")
    if not cid:
        return  # no correlation_id header — skip replay check

    now = time.monotonic()
    with _replay_lock:
        # Clean expired entries
        expired = [k for k, v in _seen_ids.items() if now - v > _replay_window]
        for k in expired:
            del _seen_ids[k]

        if cid in _seen_ids:
            raise HTTPException(
                status_code=409,
                detail="Duplicate correlation_id — possible replay attack",
            )
        _seen_ids[cid] = now


# ---------------------------------------------------------------------------
# Convenience dependency that chains all checks
# ---------------------------------------------------------------------------

async def require_auth(
    _auth: None = Depends(check_auth),
    _ip: None = Depends(check_ip),
    _replay: None = Depends(check_replay),
) -> None:
    """Apply all three security checks in one dependency."""
    pass


# ---------------------------------------------------------------------------
# State accessor helpers — used by routes to pull from app.state
# ---------------------------------------------------------------------------

def get_ami(request: Request):
    """Get the AMIClient from app state."""
    return request.app.state.ami


def get_cfg(request: Request):
    """Get config from app state."""
    return request.app.state.cfg


def get_audit(request: Request):
    """Get AuditLogger from app state."""
    return request.app.state.audit


def get_sms_limiter(request: Request):
    """Get SMS RateLimiter from app state."""
    return request.app.state.sms_limiter
