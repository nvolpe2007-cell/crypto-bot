"""Tests for src/dashboard.py — the FastAPI mobile dashboard that run_all_bots.py
actually serves in production (src/bot.py / run_all_bots.py both call
`run_dashboard()`). It had zero direct test coverage: the token-gate (`_auth_ok`),
the login flow, and the auth checks on every route were all unverified.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from src import dashboard


@pytest.fixture(autouse=True)
def _reset_token(monkeypatch):
    """Every test starts with auth disabled unless it opts in, and the module
    -level _TOKEN (read once from env at import time) is restored after."""
    monkeypatch.setattr(dashboard, "_TOKEN", "")


@pytest.fixture
def client():
    return TestClient(dashboard.app)


# ── _auth_ok ────────────────────────────────────────────────────────────────────

class TestAuthOk:
    def test_no_token_configured_always_ok(self):
        dashboard._TOKEN = ""
        req = MagicMock()
        req.cookies = {}
        req.query_params = {}
        assert dashboard._auth_ok(req) is True

    def test_token_configured_matching_cookie_ok(self, monkeypatch):
        monkeypatch.setattr(dashboard, "_TOKEN", "secret123")
        req = MagicMock()
        req.cookies = {"dash_token": "secret123"}
        req.query_params = {}
        assert dashboard._auth_ok(req) is True

    def test_token_configured_matching_query_param_ok(self, monkeypatch):
        monkeypatch.setattr(dashboard, "_TOKEN", "secret123")
        req = MagicMock()
        req.cookies = {}
        req.query_params = {"token": "secret123"}
        assert dashboard._auth_ok(req) is True

    def test_token_configured_wrong_cookie_rejected(self, monkeypatch):
        monkeypatch.setattr(dashboard, "_TOKEN", "secret123")
        req = MagicMock()
        req.cookies = {"dash_token": "wrong"}
        req.query_params = {}
        assert dashboard._auth_ok(req) is False

    def test_token_configured_nothing_supplied_rejected(self, monkeypatch):
        monkeypatch.setattr(dashboard, "_TOKEN", "secret123")
        req = MagicMock()
        req.cookies = {}
        req.query_params = {}
        assert dashboard._auth_ok(req) is False


# ── GET / ───────────────────────────────────────────────────────────────────────

class TestIndexRoute:
    def test_auth_disabled_serves_dashboard_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "CRYPTO BOT" in resp.text
        assert "<form" not in resp.text  # the real dashboard, not the login page

    def test_auth_enabled_no_token_serves_login_page(self, client, monkeypatch):
        monkeypatch.setattr(dashboard, "_TOKEN", "secret123")
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Access token" in resp.text
        assert '<form method="post" action="/login">' in resp.text

    def test_auth_enabled_valid_cookie_serves_dashboard_html(self, client, monkeypatch):
        monkeypatch.setattr(dashboard, "_TOKEN", "secret123")
        client.cookies.set("dash_token", "secret123")
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Access token" not in resp.text


# ── POST /login ──────────────────────────────────────────────────────────────────

class TestLoginRoute:
    def test_correct_token_redirects_and_sets_cookie(self, client, monkeypatch):
        monkeypatch.setattr(dashboard, "_TOKEN", "secret123")
        resp = client.post("/login", data={"token": "secret123"}, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"
        assert resp.cookies.get("dash_token") == "secret123"

    def test_wrong_token_shows_error_without_setting_cookie(self, client, monkeypatch):
        monkeypatch.setattr(dashboard, "_TOKEN", "secret123")
        resp = client.post("/login", data={"token": "nope"}, follow_redirects=False)
        assert resp.status_code == 200
        assert "Invalid token" in resp.text
        assert "dash_token" not in resp.cookies

    def test_missing_token_field_shows_error(self, client, monkeypatch):
        monkeypatch.setattr(dashboard, "_TOKEN", "secret123")
        resp = client.post("/login", data={}, follow_redirects=False)
        assert resp.status_code == 200
        assert "Invalid token" in resp.text


# ── GET /api/state ───────────────────────────────────────────────────────────────

class TestStateEndpoint:
    def test_unauthorized_returns_401(self, client, monkeypatch):
        monkeypatch.setattr(dashboard, "_TOKEN", "secret123")
        resp = client.get("/api/state")
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}

    def test_authorized_returns_read_state_payload(self, client, monkeypatch):
        monkeypatch.setattr(dashboard, "read_state", lambda: {"status": "running", "equity": 1234.5})
        resp = client.get("/api/state")
        assert resp.status_code == 200
        assert resp.json() == {"status": "running", "equity": 1234.5}


# ── GET /stream ──────────────────────────────────────────────────────────────────

class TestStreamEndpoint:
    def test_unauthorized_returns_401(self, client, monkeypatch):
        monkeypatch.setattr(dashboard, "_TOKEN", "secret123")
        resp = client.get("/stream")
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}

    @staticmethod
    def _fake_request():
        req = MagicMock()
        req.cookies = {}
        req.query_params = {}
        return req

    async def _first_chunk(self, monkeypatch, read_state_fn):
        # Call the route directly and pull one item off its async generator,
        # bypassing TestClient's blocking stream() — the route's generator
        # never terminates on its own (it loops `await asyncio.sleep(2)`
        # forever), so driving it through a real HTTP round-trip hangs.
        monkeypatch.setattr(dashboard, "read_state", read_state_fn)
        resp = await dashboard.stream(self._fake_request())
        assert resp.headers["Cache-Control"] == "no-cache"
        assert resp.headers["Access-Control-Allow-Origin"] == "*"
        return await asyncio.wait_for(resp.body_iterator.__anext__(), timeout=2)

    def test_authorized_streams_state_as_sse(self, monkeypatch):
        chunk = asyncio.run(self._first_chunk(monkeypatch, lambda: {"status": "running"}))
        assert chunk.startswith("data: ")
        assert json.loads(chunk[len("data: "):].strip()) == {"status": "running"}

    def test_read_state_failure_yields_empty_object_not_crash(self, monkeypatch):
        def _boom():
            raise RuntimeError("disk unavailable")
        chunk = asyncio.run(self._first_chunk(monkeypatch, _boom))
        assert chunk == "data: {}\n\n"


# ── run_dashboard ────────────────────────────────────────────────────────────────

class TestRunDashboard:
    def test_constructs_uvicorn_server_with_host_and_port_and_serves(self, monkeypatch):
        config_mock = MagicMock()
        server_mock = MagicMock()
        server_mock.serve = AsyncMock()
        config_cls = MagicMock(return_value=config_mock)
        server_cls = MagicMock(return_value=server_mock)
        monkeypatch.setattr(dashboard.uvicorn, "Config", config_cls)
        monkeypatch.setattr(dashboard.uvicorn, "Server", server_cls)

        asyncio.run(dashboard.run_dashboard(host="127.0.0.1", port=9999))

        config_cls.assert_called_once_with(
            dashboard.app, host="127.0.0.1", port=9999, log_level="warning"
        )
        server_cls.assert_called_once_with(config_mock)
        server_mock.serve.assert_awaited_once()
