"""Tests for ``app/runner/daily_rooms.py``.

Mocks the aiohttp session at the client level. Verifies request
shapes for each room type, error handling, and session reuse.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.runner.daily_rooms import (
    DAILY_API_BASE,
    DailyAPIError,
    DailyRoom,
    DailyRoomClient,
)

# ── Helpers ───────────────────────────────────────────────────────────────


def _build_session_mock(post_responses):
    """Build a MagicMock aiohttp session whose ``post()`` yields the given
    response objects in order. Each spec is a dict with ``status`` and
    ``body`` (string)."""

    class _MockResponse:
        def __init__(self, status, body):
            self.status = status
            self._body = body

        async def text(self):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    iterator = iter(post_responses)

    def _post(url, **kwargs):
        try:
            spec = next(iterator)
        except StopIteration as exc:
            raise AssertionError("More POSTs than mocked responses") from exc
        return _MockResponse(spec["status"], spec.get("body", ""))

    session = MagicMock()
    session.post = MagicMock(side_effect=_post)
    session.closed = False
    session.close = AsyncMock()
    return session


def _client(*, with_recording: bool = True) -> DailyRoomClient:
    if with_recording:
        return DailyRoomClient(
            api_key="test-key",
            recording_bucket="test-bucket",
            recording_role_arn="arn:aws:iam::000:role/Daily",
            recording_region="us-east-1",
        )
    return DailyRoomClient(api_key="test-key")


# ── Construction ──────────────────────────────────────────────────────────


def test_constructor_rejects_empty_api_key():
    with pytest.raises(ValueError, match="api_key"):
        DailyRoomClient(api_key="")


def test_constructor_defaults():
    c = DailyRoomClient(api_key="x")
    assert c._api_url == DAILY_API_BASE
    assert c._recording_bucket == ""
    assert c._recording_role_arn == ""


# ── create_inbound_room ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_inbound_room_request_shape():
    captured = {}

    class _MockResponse:
        status = 200

        async def text(self):
            return json.dumps({"url": "https://x.daily.co/r1", "name": "r1", "sip_uri": "sip:r1@x"})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    def _post(url, json=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        return _MockResponse()

    session = MagicMock()
    session.post = MagicMock(side_effect=_post)
    session.closed = False
    session.close = AsyncMock()

    c = _client()
    with patch.object(c, "_ensure_session", AsyncMock(return_value=session)):
        room = await c.create_inbound_room()

    assert room.url == "https://x.daily.co/r1"
    assert room.name == "r1"
    assert room.sip_uri == "sip:r1@x"
    assert captured["url"] == f"{DAILY_API_BASE}/rooms"

    payload = captured["json"]
    assert payload["privacy"] == "private"
    props = payload["properties"]
    assert props["sip"]["sip_mode"] == "dial-in"
    assert props["sip"]["video"] is False
    assert props["enable_recording"] == "cloud"
    assert props["recordings_bucket"]["bucket_name"] == "test-bucket"
    assert props["recordings_bucket"]["assume_role_arn"] == "arn:aws:iam::000:role/Daily"


@pytest.mark.asyncio
async def test_create_inbound_room_without_recording_config_omits_recording_block():
    """Default constructor (no bucket / role) → no enable_recording field."""
    captured = {}

    class _MockResponse:
        status = 200

        async def text(self):
            return json.dumps({"url": "https://x.daily.co/r1", "name": "r1"})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    def _post(url, json=None, **kwargs):
        captured["json"] = json
        return _MockResponse()

    session = MagicMock()
    session.post = MagicMock(side_effect=_post)
    session.closed = False
    session.close = AsyncMock()

    c = _client(with_recording=False)
    with patch.object(c, "_ensure_session", AsyncMock(return_value=session)):
        await c.create_inbound_room()

    props = captured["json"]["properties"]
    assert "enable_recording" not in props
    assert "recordings_bucket" not in props


# ── create_outbound_room ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_outbound_room_request_shape():
    captured = {}

    class _MockResponse:
        status = 200

        async def text(self):
            return json.dumps({"url": "https://x.daily.co/r2", "name": "r2"})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    def _post(url, json=None, **kwargs):
        captured["json"] = json
        return _MockResponse()

    session = MagicMock()
    session.post = MagicMock(side_effect=_post)
    session.closed = False
    session.close = AsyncMock()

    c = _client()
    with patch.object(c, "_ensure_session", AsyncMock(return_value=session)):
        room = await c.create_outbound_room()

    assert room.sip_uri is None
    props = captured["json"]["properties"]
    assert props["enable_dialout"] is True
    assert props["dialout_config"]["allow_room_start"] is True
    assert "sip" not in props


# ── create_browser_room ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_browser_room_request_shape():
    captured = {}

    class _MockResponse:
        status = 200

        async def text(self):
            return json.dumps({"url": "https://x.daily.co/r3", "name": "r3"})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    def _post(url, json=None, **kwargs):
        captured["json"] = json
        return _MockResponse()

    session = MagicMock()
    session.post = MagicMock(side_effect=_post)
    session.closed = False
    session.close = AsyncMock()

    c = _client()  # bucket configured, but browser shouldn't include it
    with patch.object(c, "_ensure_session", AsyncMock(return_value=session)):
        await c.create_browser_room()

    props = captured["json"]["properties"]
    assert "sip" not in props
    assert "enable_dialout" not in props
    # Browser rooms do NOT enable recording; test calls don't need it.
    assert "enable_recording" not in props
    assert "recordings_bucket" not in props


# ── mint_token ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mint_token_request_shape_owner():
    captured = {}

    class _MockResponse:
        status = 200

        async def text(self):
            return json.dumps({"token": "jwt.token.here"})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    def _post(url, json=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        return _MockResponse()

    session = MagicMock()
    session.post = MagicMock(side_effect=_post)
    session.closed = False
    session.close = AsyncMock()

    c = _client()
    with patch.object(c, "_ensure_session", AsyncMock(return_value=session)):
        token = await c.mint_token("room-abc")

    assert token == "jwt.token.here"
    assert captured["url"] == f"{DAILY_API_BASE}/meeting-tokens"
    props = captured["json"]["properties"]
    assert props["room_name"] == "room-abc"
    assert props["is_owner"] is True


@pytest.mark.asyncio
async def test_mint_token_supports_non_owner():
    class _MockResponse:
        status = 200

        async def text(self):
            return json.dumps({"token": "viewer.jwt"})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    captured = {}

    def _post(url, json=None, **kwargs):
        captured["json"] = json
        return _MockResponse()

    session = MagicMock()
    session.post = MagicMock(side_effect=_post)
    session.closed = False
    session.close = AsyncMock()

    c = _client()
    with patch.object(c, "_ensure_session", AsyncMock(return_value=session)):
        token = await c.mint_token("room-x", is_owner=False, exp_secs=900)

    assert token == "viewer.jwt"
    assert captured["json"]["properties"]["is_owner"] is False


# ── Error paths ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_room_raises_on_4xx():
    session = _build_session_mock([{"status": 401, "body": '{"error": "unauth"}'}])
    c = _client()
    with patch.object(c, "_ensure_session", AsyncMock(return_value=session)):
        with pytest.raises(DailyAPIError, match="401"):
            await c.create_inbound_room()


@pytest.mark.asyncio
async def test_create_room_raises_on_5xx():
    session = _build_session_mock([{"status": 500, "body": "internal"}])
    c = _client()
    with patch.object(c, "_ensure_session", AsyncMock(return_value=session)):
        with pytest.raises(DailyAPIError, match="500"):
            await c.create_outbound_room()


@pytest.mark.asyncio
async def test_create_room_raises_on_malformed_json():
    session = _build_session_mock([{"status": 200, "body": "not json"}])
    c = _client()
    with patch.object(c, "_ensure_session", AsyncMock(return_value=session)):
        with pytest.raises(DailyAPIError, match="non-JSON"):
            await c.create_browser_room()


@pytest.mark.asyncio
async def test_mint_token_raises_on_failure():
    session = _build_session_mock([{"status": 403, "body": '{"error": "forbidden"}'}])
    c = _client()
    with patch.object(c, "_ensure_session", AsyncMock(return_value=session)):
        with pytest.raises(DailyAPIError, match="403"):
            await c.mint_token("room-abc")


# ── Lifecycle ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_closes_session():
    c = _client()
    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.close = AsyncMock()
    c._session = fake_session
    await c.close()
    fake_session.close.assert_awaited_once()
    assert c._session is None


@pytest.mark.asyncio
async def test_close_is_safe_with_no_session():
    c = _client()
    await c.close()
    assert c._session is None


# ── DailyRoom dataclass ───────────────────────────────────────────────────


def test_daily_room_dataclass_immutable():
    room = DailyRoom(url="https://x.daily.co/r", name="r")
    assert room.sip_uri is None
    with pytest.raises((AttributeError, TypeError)):
        room.url = "modified"  # type: ignore[misc]
