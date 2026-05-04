"""Tests for ``app/runner/protection.py``.

Tests fake the ECS agent endpoint via ``aioresponses`` (or a
hand-rolled aiohttp app for in-process testing). The endpoint is
local in production (``$ECS_AGENT_URI``); we never hit a real
service from a test.

Coverage matches v1's test suite. Each test isolates one concern:
acquire / renew / retry / escalation / unavailable.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientResponseError
from app.runner.protection import (
    MAX_RETRIES,
    PROTECTION_EXPIRY_MINUTES,
    RENEWAL_ESCALATION_THRESHOLD,
    TaskProtection,
)

# ── Helpers ─────────────────────────────────────────────────────────────


def _build_session_mock(responses):
    """Build a MagicMock aiohttp session whose ``put()`` yields the
    given response objects in order. Each response object is an
    object exposing ``.status`` and ``.text()``.
    """

    class _MockResponse:
        def __init__(self, status, text=""):
            self.status = status
            self._text = text

        async def text(self):
            return self._text

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    iterator = iter(responses)

    def _put(*args, **kwargs):
        try:
            r = next(iterator)
        except StopIteration as exc:
            raise AssertionError("More PUTs than mocked responses") from exc
        if isinstance(r, Exception):
            raise r
        return _MockResponse(r["status"], r.get("body", ""))

    session = MagicMock()
    session.put = MagicMock(side_effect=_put)
    session.closed = False
    session.close = AsyncMock()
    return session


# ── is_available — local dev / production toggle ────────────────────────


def test_is_available_false_without_env(monkeypatch):
    monkeypatch.delenv("ECS_AGENT_URI", raising=False)
    p = TaskProtection()
    assert p.is_available is False


def test_is_available_true_with_env(monkeypatch):
    monkeypatch.setenv("ECS_AGENT_URI", "http://localhost:51678")
    p = TaskProtection()
    assert p.is_available is True


# ── set_protected — local dev no-op ─────────────────────────────────────


@pytest.mark.asyncio
async def test_set_protected_returns_false_when_unavailable(monkeypatch):
    monkeypatch.delenv("ECS_AGENT_URI", raising=False)
    p = TaskProtection()
    assert await p.set_protected(True) is False
    assert p.is_protected is False


# ── set_protected — happy path ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_protected_acquires_on_200(monkeypatch):
    monkeypatch.setenv("ECS_AGENT_URI", "http://localhost:51678")
    p = TaskProtection()
    session = _build_session_mock([{"status": 200}])
    with patch.object(p, "_get_session", AsyncMock(return_value=session)):
        ok = await p.set_protected(True)
    assert ok is True
    assert p.is_protected is True
    # Verify the request payload included ExpiresInMinutes when acquiring.
    call_args = session.put.call_args
    payload = call_args.kwargs.get("json") or call_args.args[1]
    assert payload["ProtectionEnabled"] is True
    assert payload["ExpiresInMinutes"] == PROTECTION_EXPIRY_MINUTES


@pytest.mark.asyncio
async def test_set_protected_releases_on_200(monkeypatch):
    monkeypatch.setenv("ECS_AGENT_URI", "http://localhost:51678")
    p = TaskProtection()
    p._protected = True  # pre-set for the release test
    session = _build_session_mock([{"status": 200}])
    with patch.object(p, "_get_session", AsyncMock(return_value=session)):
        ok = await p.set_protected(False)
    assert ok is True
    assert p.is_protected is False
    # Release omits ExpiresInMinutes per AWS API.
    call_args = session.put.call_args
    payload = call_args.kwargs.get("json") or call_args.args[1]
    assert payload["ProtectionEnabled"] is False
    assert "ExpiresInMinutes" not in payload


@pytest.mark.asyncio
async def test_set_protected_no_change_short_circuits(monkeypatch):
    monkeypatch.setenv("ECS_AGENT_URI", "http://localhost:51678")
    p = TaskProtection()
    p._protected = True
    # No PUT should fire; if one does, the empty response list raises.
    session = _build_session_mock([])
    with patch.object(p, "_get_session", AsyncMock(return_value=session)):
        ok = await p.set_protected(True)
    assert ok is True
    session.put.assert_not_called()


# ── set_protected — retry logic ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_protected_retries_on_5xx(monkeypatch):
    monkeypatch.setenv("ECS_AGENT_URI", "http://localhost:51678")
    p = TaskProtection()
    session = _build_session_mock(
        [
            {"status": 503, "body": "service unavailable"},
            {"status": 200},
        ]
    )
    with patch.object(p, "_get_session", AsyncMock(return_value=session)):
        ok = await p.set_protected(True)
    assert ok is True
    assert session.put.call_count == 2


@pytest.mark.asyncio
async def test_set_protected_returns_false_after_all_retries(monkeypatch):
    monkeypatch.setenv("ECS_AGENT_URI", "http://localhost:51678")
    p = TaskProtection()
    session = _build_session_mock([{"status": 500, "body": "x"}] * MAX_RETRIES)
    with patch.object(p, "_get_session", AsyncMock(return_value=session)):
        ok = await p.set_protected(True)
    assert ok is False
    assert session.put.call_count == MAX_RETRIES


@pytest.mark.asyncio
async def test_set_protected_no_retry_when_disabled(monkeypatch):
    monkeypatch.setenv("ECS_AGENT_URI", "http://localhost:51678")
    p = TaskProtection()
    session = _build_session_mock([{"status": 500, "body": "x"}])
    with patch.object(p, "_get_session", AsyncMock(return_value=session)):
        ok = await p.set_protected(True, retry=False)
    assert ok is False
    assert session.put.call_count == 1


@pytest.mark.asyncio
async def test_set_protected_handles_network_exception(monkeypatch):
    monkeypatch.setenv("ECS_AGENT_URI", "http://localhost:51678")
    p = TaskProtection()
    session = _build_session_mock(
        [
            ClientResponseError(MagicMock(), MagicMock(), status=500),
            ClientResponseError(MagicMock(), MagicMock(), status=500),
            {"status": 200},
        ]
    )
    with patch.object(p, "_get_session", AsyncMock(return_value=session)):
        ok = await p.set_protected(True)
    assert ok is True


# ── renew_if_protected ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_renew_returns_false_when_not_protected(monkeypatch):
    monkeypatch.setenv("ECS_AGENT_URI", "http://localhost:51678")
    p = TaskProtection()
    assert p.is_protected is False
    ok = await p.renew_if_protected()
    assert ok is False


@pytest.mark.asyncio
async def test_renew_returns_false_when_unavailable(monkeypatch):
    monkeypatch.delenv("ECS_AGENT_URI", raising=False)
    p = TaskProtection()
    p._protected = True
    ok = await p.renew_if_protected()
    assert ok is False


@pytest.mark.asyncio
async def test_renew_succeeds_on_200(monkeypatch):
    monkeypatch.setenv("ECS_AGENT_URI", "http://localhost:51678")
    p = TaskProtection()
    p._protected = True
    session = _build_session_mock([{"status": 200}])
    with patch.object(p, "_get_session", AsyncMock(return_value=session)):
        ok = await p.renew_if_protected()
    assert ok is True
    assert p._consecutive_renewal_failures == 0


@pytest.mark.asyncio
async def test_renew_logs_recovered_after_prior_failures(monkeypatch):
    monkeypatch.setenv("ECS_AGENT_URI", "http://localhost:51678")
    p = TaskProtection()
    p._protected = True
    p._consecutive_renewal_failures = 2
    session = _build_session_mock([{"status": 200}])
    with patch.object(p, "_get_session", AsyncMock(return_value=session)):
        ok = await p.renew_if_protected()
    assert ok is True
    assert p._consecutive_renewal_failures == 0


@pytest.mark.asyncio
async def test_renew_increments_failure_counter_after_retries(monkeypatch):
    monkeypatch.setenv("ECS_AGENT_URI", "http://localhost:51678")
    p = TaskProtection()
    p._protected = True
    session = _build_session_mock([{"status": 500, "body": "x"}] * MAX_RETRIES)
    with patch.object(p, "_get_session", AsyncMock(return_value=session)):
        ok = await p.renew_if_protected()
    assert ok is False
    assert p._consecutive_renewal_failures == 1


@pytest.mark.asyncio
async def test_renew_escalates_after_threshold(monkeypatch, caplog):
    monkeypatch.setenv("ECS_AGENT_URI", "http://localhost:51678")
    p = TaskProtection()
    p._protected = True
    p._consecutive_renewal_failures = RENEWAL_ESCALATION_THRESHOLD - 1

    session = _build_session_mock([{"status": 500, "body": "x"}] * MAX_RETRIES)
    with patch.object(p, "_get_session", AsyncMock(return_value=session)):
        await p.renew_if_protected()

    # Counter should now be at threshold; that's the trigger for the
    # ERROR log.
    assert p._consecutive_renewal_failures == RENEWAL_ESCALATION_THRESHOLD


# ── close() cleanup ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_closes_session():
    p = TaskProtection()
    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.close = AsyncMock()
    p._session = fake_session
    await p.close()
    fake_session.close.assert_awaited_once()
    assert p._session is None


@pytest.mark.asyncio
async def test_close_is_safe_when_no_session():
    p = TaskProtection()
    # Should not raise.
    await p.close()
    assert p._session is None


@pytest.mark.asyncio
async def test_close_skipped_when_session_already_closed():
    p = TaskProtection()
    fake_session = MagicMock()
    fake_session.closed = True
    fake_session.close = AsyncMock()
    p._session = fake_session
    await p.close()
    fake_session.close.assert_not_awaited()
