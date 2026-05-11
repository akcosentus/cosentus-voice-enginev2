"""Tests for ``app/runner/server.py`` — HTTP routes + graceful drain.

Uses ``aiohttp.test_utils.TestClient`` (via pytest-aiohttp's
``aiohttp_client`` fixture) when needed; for most tests we
construct the app + client directly inside each test.

The PipelineManager is mocked at the methods level so we don't
spin up real bots. Daily client + protection are also mocked.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from app.config.settings import Settings
from app.runner.manager import CallSpawnResult, CapacityRejected, PipelineManager
from app.runner.server import (
    DEFAULT_DRAIN_BUDGET_SECS,
    build_app,
    graceful_drain,
)

# ── Fixtures ──────────────────────────────────────────────────────────────


def _settings(*, api_key_arn: str = "") -> Settings:
    return Settings(
        voice_api_lambda_name="test-voice-api",
        api_key_secret_arn=api_key_arn,
        max_concurrent_calls=6,
    )


def _manager_mock() -> MagicMock:
    m = MagicMock(spec=PipelineManager)
    m.is_draining = False
    m.at_capacity = False
    m.active_session_count = 0
    m.active_sessions = {}
    m.get_status = MagicMock(
        return_value={
            "active_sessions": 0,
            "max_concurrent": 6,
            "draining": False,
            "protected": False,
            "protection_available": False,
        }
    )
    m.start_outbound = AsyncMock()
    m.start_browser = AsyncMock()
    m.start_inbound = AsyncMock()
    m.shutdown = AsyncMock()
    return m


async def _build_app_for_test(
    manager: MagicMock,
    *,
    api_key: str = "",
    settings: Settings | None = None,
) -> web.Application:
    """Build the aiohttp app, sidestepping the real Secrets Manager call."""
    settings = settings or _settings()
    with patch("app.runner.server._load_api_key", AsyncMock(return_value=api_key)):
        return await build_app(settings, manager)


# ── /health ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_always_200(aiohttp_client):
    manager = _manager_mock()
    app = await _build_app_for_test(manager)
    client = await aiohttp_client(app)
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "healthy"


# ── /ready ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ready_200_when_healthy(aiohttp_client):
    manager = _manager_mock()
    app = await _build_app_for_test(manager)
    client = await aiohttp_client(app)
    resp = await client.get("/ready")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ready"


@pytest.mark.asyncio
async def test_ready_503_when_draining(aiohttp_client):
    manager = _manager_mock()
    manager.is_draining = True
    app = await _build_app_for_test(manager)
    client = await aiohttp_client(app)
    resp = await client.get("/ready")
    assert resp.status == 503
    body = await resp.json()
    assert body["status"] == "draining"


@pytest.mark.asyncio
async def test_ready_503_when_at_capacity(aiohttp_client):
    manager = _manager_mock()
    manager.at_capacity = True
    app = await _build_app_for_test(manager)
    client = await aiohttp_client(app)
    resp = await client.get("/ready")
    assert resp.status == 503
    body = await resp.json()
    assert body["status"] == "at_capacity"


# ── /status ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_requires_auth_when_key_configured(aiohttp_client):
    manager = _manager_mock()
    app = await _build_app_for_test(manager, api_key="secret-key")
    client = await aiohttp_client(app)
    resp = await client.get("/status")
    assert resp.status == 401


@pytest.mark.asyncio
async def test_status_returns_with_valid_key(aiohttp_client):
    manager = _manager_mock()
    app = await _build_app_for_test(manager, api_key="secret-key")
    client = await aiohttp_client(app)
    resp = await client.get("/status", headers={"X-API-Key": "secret-key"})
    assert resp.status == 200
    body = await resp.json()
    assert body["max_concurrent"] == 6


@pytest.mark.asyncio
async def test_status_open_in_local_dev(aiohttp_client):
    """Empty api_key_secret_arn → no auth required."""
    manager = _manager_mock()
    app = await _build_app_for_test(manager, api_key="")
    client = await aiohttp_client(app)
    resp = await client.get("/status")
    assert resp.status == 200


# ── /start outbound ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_outbound_returns_202(aiohttp_client):
    manager = _manager_mock()
    manager.start_outbound = AsyncMock(
        return_value=CallSpawnResult(call_id="call-1", room_name="r1", room_url="https://x/r1")
    )
    app = await _build_app_for_test(manager)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/start",
        json={
            "direction": "outbound",
            "agent_id": "agent-1",
            "target_number": "+19494360836",
            "from_number": "+12098075018",
            "case_data": {"k": "v"},
        },
    )
    assert resp.status == 202
    body = await resp.json()
    assert body["call_id"] == "call-1"
    assert body["status"] == "started"
    assert "viewer_token" not in body


@pytest.mark.asyncio
async def test_start_outbound_400_without_agent_id(aiohttp_client):
    manager = _manager_mock()
    app = await _build_app_for_test(manager)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/start",
        json={"direction": "outbound", "target_number": "+1", "from_number": "+1"},
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "agent_id_required"


@pytest.mark.asyncio
async def test_start_outbound_400_without_target_number(aiohttp_client):
    manager = _manager_mock()
    app = await _build_app_for_test(manager)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/start",
        json={"direction": "outbound", "agent_id": "x", "from_number": "+1"},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_start_503_when_at_capacity(aiohttp_client):
    manager = _manager_mock()
    manager.start_outbound = AsyncMock(side_effect=CapacityRejected("at_capacity"))
    app = await _build_app_for_test(manager)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/start",
        json={
            "direction": "outbound",
            "agent_id": "a",
            "target_number": "+1",
            "from_number": "+1",
        },
    )
    assert resp.status == 503
    body = await resp.json()
    assert body["reason"] == "at_capacity"


# ── /start browser ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_browser_returns_viewer_token(aiohttp_client):
    manager = _manager_mock()
    manager.start_browser = AsyncMock(
        return_value=CallSpawnResult(
            call_id="call-b",
            room_name="rb",
            room_url="https://x/rb",
            viewer_token="viewer.jwt",
        )
    )
    app = await _build_app_for_test(manager)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/start",
        json={"direction": "browser", "agent_id": "a"},
    )
    assert resp.status == 202
    body = await resp.json()
    assert body["viewer_token"] == "viewer.jwt"


@pytest.mark.asyncio
async def test_start_400_unknown_direction(aiohttp_client):
    manager = _manager_mock()
    app = await _build_app_for_test(manager)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/start",
        json={"direction": "carrier-pigeon", "agent_id": "a"},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_start_400_invalid_json(aiohttp_client):
    manager = _manager_mock()
    app = await _build_app_for_test(manager)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/start", data="not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status == 400


# ── /daily-dialin-webhook ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dialin_webhook_with_valid_lookup(aiohttp_client):
    manager = _manager_mock()
    manager.start_inbound = AsyncMock(
        return_value=CallSpawnResult(call_id="call-in", room_name="rin", room_url="https://x/rin")
    )
    app = await _build_app_for_test(manager)
    client = await aiohttp_client(app)
    with patch(
        "app.runner.server._lookup_inbound_agent",
        AsyncMock(return_value="agent-id-from-lambda"),
    ):
        resp = await client.post(
            "/daily-dialin-webhook",
            json={
                "From": "+19494360836",
                "To": "+12098075018",
                "callId": "ext-call",
                "callDomain": "x.daily.co",
            },
        )
    assert resp.status == 200
    body = await resp.json()
    assert body["dailyRoom"] == "https://x/rin"
    assert body["sessionId"] == "call-in"


@pytest.mark.asyncio
async def test_dialin_webhook_503_when_agent_lookup_fails(aiohttp_client):
    manager = _manager_mock()
    app = await _build_app_for_test(manager)
    client = await aiohttp_client(app)
    with patch(
        "app.runner.server._lookup_inbound_agent",
        AsyncMock(return_value=None),
    ):
        resp = await client.post(
            "/daily-dialin-webhook",
            json={"From": "+1", "To": "+1234567890", "callId": "x", "callDomain": "y"},
        )
    assert resp.status == 503
    body = await resp.json()
    assert body["error"] == "no_agent_configured"


@pytest.mark.asyncio
async def test_dialin_webhook_400_without_to(aiohttp_client):
    manager = _manager_mock()
    app = await _build_app_for_test(manager)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/daily-dialin-webhook",
        json={"From": "+1", "callId": "x", "callDomain": "y"},
    )
    assert resp.status == 400


# ── _lookup_inbound_agent: real lambda envelope shape ────────────────────
#
# These tests exercise the actual JSON parsing in
# ``_lookup_inbound_agent`` against the exact response shape
# emitted by the medcloud-voice-api lambda — the gap that hid the
# Phase 2 inbound-test blocker. The webhook integration tests
# above stub ``_lookup_inbound_agent`` itself, so they're blind to
# field-name mismatches between Layer 9 and the API surface.


def _build_lambda_invoke_response(*, status_code: int = 200, body: dict | None = None) -> MagicMock:
    """Build the boto3 ``client.invoke()`` return shape.

    ``Payload`` is a ``StreamingBody``-like object exposing ``read()``;
    ``read()`` returns the JSON-encoded API Gateway envelope (the
    lambda is API-Gateway-shaped under the hood).
    """
    import json as _json

    envelope = {
        "statusCode": status_code,
        "body": _json.dumps(body) if body is not None else "{}",
        "headers": {"Content-Type": "application/json"},
    }
    payload_bytes = _json.dumps(envelope).encode("utf-8")
    payload_mock = MagicMock()
    payload_mock.read = MagicMock(return_value=payload_bytes)
    return {"Payload": payload_mock}


@pytest.mark.asyncio
async def test_lookup_inbound_agent_parses_nested_inbound_agent_object():
    """The lambda returns body.inbound_agent.{id,name,display_name}.
    Layer 9 must reach into the nested ``id`` field — the prior
    code read flat ``inbound_agent_id`` and silently failed every
    real inbound call.
    """
    from app.runner.server import _lookup_inbound_agent

    real_shape = {
        "id": "72627663-c42f-4e26-8a5a-26bff07a561e",
        "number": "+12098075018",
        "friendly_name": "Dev Daily test",
        "provider": "daily",
        "is_active": True,
        "inbound_agent": {
            "id": "576b22a4-42ad-4ac1-8a2b-7067fb5c5cd4",
            "name": "chris-claim-status",
            "display_name": "Chris Claim Status",
        },
        "outbound_agent": None,
    }
    lambda_client_mock = MagicMock()
    lambda_client_mock.invoke = MagicMock(
        return_value=_build_lambda_invoke_response(body=real_shape)
    )

    with patch(
        "app.config.agent_config._get_lambda_client",
        return_value=lambda_client_mock,
    ):
        result = await _lookup_inbound_agent("+12098075018", _settings())

    assert result == "576b22a4-42ad-4ac1-8a2b-7067fb5c5cd4"


@pytest.mark.asyncio
async def test_lookup_inbound_agent_returns_none_when_inbound_agent_null():
    """Number is provisioned but has no inbound mapping (e.g., this
    number is outbound-only: ``inbound_agent: null``)."""
    from app.runner.server import _lookup_inbound_agent

    outbound_only_shape = {
        "id": "abc",
        "number": "+19998887777",
        "is_active": True,
        "inbound_agent": None,
        "outbound_agent": {
            "id": "outbound-agent-id",
            "name": "outbound-only-agent",
        },
    }
    lambda_client_mock = MagicMock()
    lambda_client_mock.invoke = MagicMock(
        return_value=_build_lambda_invoke_response(body=outbound_only_shape)
    )

    with patch(
        "app.config.agent_config._get_lambda_client",
        return_value=lambda_client_mock,
    ):
        result = await _lookup_inbound_agent("+19998887777", _settings())

    assert result is None


@pytest.mark.asyncio
async def test_lookup_inbound_agent_returns_none_on_404():
    """Unknown phone number — lambda returns 404 with detail."""
    from app.runner.server import _lookup_inbound_agent

    lambda_client_mock = MagicMock()
    lambda_client_mock.invoke = MagicMock(
        return_value=_build_lambda_invoke_response(
            status_code=404, body={"detail": "Phone number not found"}
        )
    )

    with patch(
        "app.config.agent_config._get_lambda_client",
        return_value=lambda_client_mock,
    ):
        result = await _lookup_inbound_agent("+10000000000", _settings())

    assert result is None


@pytest.mark.asyncio
async def test_lookup_inbound_agent_returns_none_when_id_empty_string():
    """Defensive: lambda returns ``inbound_agent: {}`` or ``inbound_agent: {"id": ""}``."""
    from app.runner.server import _lookup_inbound_agent

    lambda_client_mock = MagicMock()
    lambda_client_mock.invoke = MagicMock(
        return_value=_build_lambda_invoke_response(
            body={
                "id": "x",
                "number": "+1",
                "is_active": True,
                "inbound_agent": {"id": ""},
            }
        )
    )

    with patch(
        "app.config.agent_config._get_lambda_client",
        return_value=lambda_client_mock,
    ):
        result = await _lookup_inbound_agent("+1", _settings())

    assert result is None


# ── graceful_drain ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_sets_draining_flag():
    manager = _manager_mock()
    protection = MagicMock()
    protection.set_protected = AsyncMock()
    protection.close = AsyncMock()

    await graceful_drain(manager, protection, budget_secs=1)
    manager.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_drain_releases_protection():
    manager = _manager_mock()
    protection = MagicMock()
    protection.set_protected = AsyncMock()
    protection.close = AsyncMock()

    await graceful_drain(manager, protection, budget_secs=1)
    protection.set_protected.assert_awaited_with(False)
    protection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_drain_cancels_only_active_sessions_not_all_tasks():
    """Critical fix from v1: drain MUST cancel only manager's tasks,
    not asyncio.all_tasks(loop) which would kill the HTTP server."""
    manager = _manager_mock()
    protection = MagicMock()
    protection.set_protected = AsyncMock()
    protection.close = AsyncMock()

    # Simulate one active session that exceeds the drain budget.
    bot_event = asyncio.Event()

    async def slow_bot():
        try:
            await bot_event.wait()
        except asyncio.CancelledError:
            raise

    fake_task = asyncio.create_task(slow_bot(), name="fake-call-task")
    # Track an unrelated task too — drain must NOT cancel this.
    untouched_event = asyncio.Event()

    async def untouched_runner():
        await untouched_event.wait()

    untouched_task = asyncio.create_task(untouched_runner(), name="unrelated-task")

    # Wire the active_sessions dict + active_session_count.
    manager._active_sessions = {"call-1": fake_task}
    manager.active_sessions = manager._active_sessions
    # active_session_count returns the dict length each time.
    type(manager).active_session_count = property(lambda self: len(self.active_sessions))

    try:
        await graceful_drain(manager, protection, budget_secs=0)
    finally:
        # Cleanup unrelated task.
        untouched_event.set()
        await untouched_task

    # Fake task was cancelled (active session); unrelated task was NOT.
    assert fake_task.cancelled() or fake_task.done()
    assert not untouched_task.cancelled()


@pytest.mark.asyncio
async def test_drain_default_budget_is_110():
    """Sanity: matches Fargate's 120s default stopTimeout minus 10s buffer."""
    assert DEFAULT_DRAIN_BUDGET_SECS == 110


# ── graceful_drain ↔ MetricsEmitter wiring ───────────────────────────────


@pytest.mark.asyncio
async def test_drain_calls_emit_drain_timeout_on_timeout():
    """Wave 4: on the budget-exceeded branch, DrainTimeouts must be
    published so Wave 3's alarm has a datapoint to evaluate."""
    manager = _manager_mock()
    protection = MagicMock()
    protection.set_protected = AsyncMock()
    protection.close = AsyncMock()

    bot_event = asyncio.Event()

    async def slow_bot():
        try:
            await bot_event.wait()
        except asyncio.CancelledError:
            raise

    fake_task = asyncio.create_task(slow_bot(), name="slow-bot")
    manager._active_sessions = {"call-1": fake_task}
    manager.active_sessions = manager._active_sessions
    type(manager).active_session_count = property(lambda self: len(self.active_sessions))

    metrics = MagicMock()
    metrics.emit_drain_timeout = AsyncMock()

    try:
        await graceful_drain(manager, protection, budget_secs=0, metrics=metrics)
    finally:
        bot_event.set()

    # The emitter saw exactly one drain-timeout event with the active
    # session count at the moment of timeout (= 1, the stuck session).
    metrics.emit_drain_timeout.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_drain_does_not_emit_when_no_timeout():
    """When drain completes within budget, no DrainTimeouts event fires."""
    manager = _manager_mock()
    protection = MagicMock()
    protection.set_protected = AsyncMock()
    protection.close = AsyncMock()
    # active_session_count starts at 0 → loop exits immediately.
    metrics = MagicMock()
    metrics.emit_drain_timeout = AsyncMock()

    await graceful_drain(manager, protection, budget_secs=10, metrics=metrics)

    metrics.emit_drain_timeout.assert_not_called()


@pytest.mark.asyncio
async def test_drain_works_without_metrics_emitter():
    """Backward compatibility: callers without a MetricsEmitter (e.g.,
    older tests, local dev) must still see drain complete cleanly."""
    manager = _manager_mock()
    protection = MagicMock()
    protection.set_protected = AsyncMock()
    protection.close = AsyncMock()

    # Should not raise even though metrics=None on the timeout path.
    await graceful_drain(manager, protection, budget_secs=0)
    manager.shutdown.assert_awaited_once()
    protection.set_protected.assert_awaited_with(False)
