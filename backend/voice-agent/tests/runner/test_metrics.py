"""Tests for ``app/runner/metrics.py`` — CloudWatch metric emission."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from app.config.settings import Settings
from app.runner.manager import PipelineManager
from app.runner.metrics import MetricsEmitter


def _settings() -> Settings:
    return Settings(
        voice_api_lambda_name="test-voice-api",
        api_key_secret_arn="arn:aws:secretsmanager:us-east-1:0:secret:test",
        max_concurrent_calls=6,
    )


def _manager_mock(*, active: int = 0, max_concurrent: int = 6) -> MagicMock:
    m = MagicMock(spec=PipelineManager)
    m.active_session_count = active
    m.get_status = MagicMock(
        return_value={
            "active_sessions": active,
            "max_concurrent": max_concurrent,
        }
    )
    return m


def _make_emitter(*, active: int = 0, max_concurrent: int = 6) -> tuple[MetricsEmitter, MagicMock]:
    """Build a MetricsEmitter with a mocked CloudWatch client.

    Returns (emitter, cloudwatch_client_mock).
    """
    manager = _manager_mock(active=active, max_concurrent=max_concurrent)
    settings = _settings()
    with patch("app.runner.metrics.boto3.client") as boto3_client:
        cw_mock = MagicMock()
        cw_mock.put_metric_data = MagicMock()
        boto3_client.return_value = cw_mock
        emitter = MetricsEmitter(manager, settings)
    return emitter, cw_mock


# ── _emit ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emit_puts_active_sessions_metric():
    emitter, cw = _make_emitter(active=3, max_concurrent=6)
    await emitter._emit()
    cw.put_metric_data.assert_called_once()
    kwargs = cw.put_metric_data.call_args.kwargs
    assert kwargs["Namespace"] == "VoiceAgent/Pipeline"
    metric_names = [m["MetricName"] for m in kwargs["MetricData"]]
    assert "ActiveSessions" in metric_names
    assert "SessionUtilization" in metric_names


@pytest.mark.asyncio
async def test_emit_active_sessions_value():
    emitter, cw = _make_emitter(active=4, max_concurrent=6)
    await emitter._emit()
    metrics = cw.put_metric_data.call_args.kwargs["MetricData"]
    active = next(m for m in metrics if m["MetricName"] == "ActiveSessions")
    assert active["Value"] == 4.0
    assert active["Unit"] == "Count"


@pytest.mark.asyncio
async def test_emit_utilization_percent():
    emitter, cw = _make_emitter(active=3, max_concurrent=6)
    await emitter._emit()
    metrics = cw.put_metric_data.call_args.kwargs["MetricData"]
    util = next(m for m in metrics if m["MetricName"] == "SessionUtilization")
    assert util["Value"] == 50.0
    assert util["Unit"] == "Percent"


@pytest.mark.asyncio
async def test_emit_with_task_id_dimension(monkeypatch):
    """When ECS_TASK_ID is set, datapoints carry a TaskId dimension."""
    monkeypatch.setenv("ECS_TASK_ID", "task-abc-123")
    # Re-construct so the env-var read at __init__ fires.
    emitter, cw = _make_emitter()
    await emitter._emit()
    metrics = cw.put_metric_data.call_args.kwargs["MetricData"]
    for m in metrics:
        assert m["Dimensions"] == [{"Name": "TaskId", "Value": "task-abc-123"}]


# ── start / stop lifecycle ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_creates_task_and_stop_cancels():
    emitter, _ = _make_emitter()
    await emitter.start()
    assert emitter._task is not None
    assert not emitter._task.done()
    await emitter.stop()
    assert emitter._task is None


@pytest.mark.asyncio
async def test_start_is_idempotent():
    emitter, _ = _make_emitter()
    await emitter.start()
    first_task = emitter._task
    await emitter.start()
    # Second start does not replace the running task.
    assert emitter._task is first_task
    await emitter.stop()


@pytest.mark.asyncio
async def test_stop_is_safe_when_not_started():
    emitter, _ = _make_emitter()
    # Should not raise.
    await emitter.stop()


@pytest.mark.asyncio
async def test_loop_swallows_exceptions(monkeypatch):
    """A CloudWatch outage shouldn't kill the emitter."""
    emitter, cw = _make_emitter()
    cw.put_metric_data = MagicMock(side_effect=RuntimeError("aws outage"))
    # Shorten the sleep so we can witness one error cycle quickly.
    monkeypatch.setattr("app.runner.metrics._EMIT_INTERVAL_SECS", 0.01)
    await emitter.start()
    # Let it tick a few times; if exceptions weren't swallowed the
    # task would be done by now with an exception.
    await asyncio.sleep(0.05)
    assert not emitter._task.done()
    await emitter.stop()
