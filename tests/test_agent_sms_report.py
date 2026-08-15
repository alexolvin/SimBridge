"""Agent /v1/sms/report endpoint tests (D3).

The AGI hook POSTs the raw carrier delivery-report text to the agent;
the endpoint resolves the record through the correlation store, marks
it delivered/failed, audits, and notifies the userbot (captured here by
a local HTTP server standing in for the userbot's /events/delivery).
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent import deps
from agent.routes import router
from core.audit import AuditLogger
from core.events import EventType
from core.sms_correlation import SMSCorrelationStore


# ---------------------------------------------------------------------------
# Local capture server (stands in for the userbot /events/delivery)
# ---------------------------------------------------------------------------

class _CaptureServer:
    def __init__(self) -> None:
        captured: list[dict] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                captured.append({
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": self.rfile.read(length),
                })
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args) -> None:  # silence
                pass

        self.captured = captured
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeAudit:
    def __init__(self):
        self.calls = []

    def log(self, event_type, **kw):
        self.calls.append((event_type, kw))


# ---------------------------------------------------------------------------
# Fixture: real agent router + real correlation store + captured userbot
# ---------------------------------------------------------------------------

@pytest.fixture()
def capture():
    srv = _CaptureServer()
    yield srv
    srv.stop()


@pytest.fixture()
def env(tmp_path, capture, monkeypatch):
    app = FastAPI()
    app.include_router(router, prefix="/v1")

    store = SMSCorrelationStore()
    app.state.cfg = {
        "agent.userbot_url": capture.url,
        "userbot_http.secret_env": "SIMBRIDGE_HTTP_SECRET",
    }
    app.state.sms_store = store
    app.state.audit = FakeAudit()

    monkeypatch.setenv("SIMBRIDGE_HTTP_SECRET", "sec")
    old_token, old_peers = deps._agent_token, deps._allowed_peers
    deps._agent_token = "test-token"
    # starlette TestClient connects from host "testclient"
    deps._allowed_peers = {"testclient"}

    yield TestClient(app), store, app.state.audit

    deps._agent_token, deps._allowed_peers = old_token, old_peers


def _auth():
    return {"Authorization": "Bearer test-token"}


# ---------------------------------------------------------------------------
# /v1/sms/report
# ---------------------------------------------------------------------------

class TestSmsReport:
    def test_matched_delivered_notifies_userbot(self, env, capture):
        client, store, audit = env
        rec = store.create(123, "+79261234555", "hello")
        store.mark_submitted(rec.sms_id)

        r = client.post(
            "/v1/sms/report", headers=_auth(),
            json={
                "phone_number": "carrier",
                "text": "Delivered 89261234555 2026-08-15 10:00",
                "modem_id": "gsm",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["matched"] is True
        assert body["status"] == "delivered"
        assert body["sms_id"] == rec.sms_id
        assert store.get(rec.sms_id).delivery_status == "delivered"

        # D3: the userbot is notified over HTTP with its own secret
        (req,) = capture.captured
        assert req["path"] == "/events/delivery"
        headers = {k.lower(): v for k, v in req["headers"].items()}
        assert headers["x-simbridge-secret"] == "sec"
        payload = json.loads(req["body"])
        assert payload["sms_id"] == rec.sms_id
        assert payload["phone_number"] == "+79261234555"
        assert payload["telegram_user_id"] == 123
        assert payload["status"] == "delivered"

        etype, kw = audit.calls[0]
        assert etype == EventType.SMS_DELIVERY_REPORT
        assert kw["outcome"] == "delivered"

    def test_matched_failed(self, env, capture):
        client, store, audit = env
        rec = store.create(123, "+79261234555", "hello")
        store.mark_submitted(rec.sms_id)

        r = client.post(
            "/v1/sms/report", headers=_auth(),
            json={
                "phone_number": "carrier",
                "text": "Not delivered 89261234555 — expired",
                "modem_id": "gsm",
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "failed"
        rec2 = store.get(rec.sms_id)
        assert rec2.delivery_status == "failed"
        assert "Not delivered" in rec2.error_message

        payload = json.loads(capture.captured[0]["body"])
        assert payload["status"] == "failed"
        assert payload["error"]
        assert audit.calls[0][1]["outcome"] == "failed"

    def test_no_match(self, env, capture):
        client, store, audit = env  # empty store
        r = client.post(
            "/v1/sms/report", headers=_auth(),
            json={
                "phone_number": "carrier",
                "text": "Delivered 89999999999",
                "modem_id": "gsm",
            },
        )
        assert r.status_code == 200
        assert r.json()["matched"] is False
        assert capture.captured == []  # nobody to notify
        assert audit.calls[0][1]["outcome"] == "no_match"

    def test_missing_token_401(self, env):
        client, store, audit = env
        r = client.post(
            "/v1/sms/report",
            json={"phone_number": "c", "text": "x", "modem_id": "gsm"},
        )
        assert r.status_code == 401

    def test_wrong_token_401(self, env):
        client, store, audit = env
        r = client.post(
            "/v1/sms/report",
            headers={"Authorization": "Bearer wrong"},
            json={"phone_number": "c", "text": "x", "modem_id": "gsm"},
        )
        assert r.status_code == 401
