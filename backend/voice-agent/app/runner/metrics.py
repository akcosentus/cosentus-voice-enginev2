"""CloudWatch metric emission for ECS auto-scaling.

Emits two metrics every :data:`_EMIT_INTERVAL_SECS` while the
process runs:

* ``ActiveSessions`` — current ``len(active_sessions)``. Drives the
  target-tracking auto-scaling policy. CDK (Layer 11) configures
  the scaling policy with a target like 3 sessions per task; ECS
  adds tasks when the running average crosses it.
* ``SessionUtilization`` — ``active / max_concurrent`` as a
  percentage. Diagnostic; not used by the scaling policy directly,
  but useful for dashboards + alarms.

Uses ``boto3.client("cloudwatch")`` with synchronous PUT through
:func:`asyncio.to_thread` so the emission doesn't block the event
loop. Failures log + continue — a missed metric tick shouldn't
take down the engine.
"""

from __future__ import annotations

import asyncio
import os

import boto3
import structlog

from app.config.settings import Settings
from app.runner.manager import PipelineManager

logger = structlog.get_logger(__name__)

# Match v1's cadence and AWS sample's pattern. 30 s gives a
# responsive scaling signal without flooding CloudWatch (and
# without paying for too many ``PutMetricData`` calls — each is
# billed at $0.0000010 per metric, but namespaces accumulate).
_EMIT_INTERVAL_SECS = 30

# All metrics live under this namespace. CDK (Layer 11) wires the
# auto-scaling policy and dashboards against it.
_METRIC_NAMESPACE = "VoiceAgent/Pipeline"

# Optional dimension. When the ECS task ID is available (from
# ``ECS_CONTAINER_METADATA_URI_V4`` or v1's ``get_ecs_task_id``)
# we tag every datapoint with it so per-task metrics are
# distinguishable in CloudWatch. Unset in local dev.
_TASK_ID_ENV = "ECS_TASK_ID"


def _resolve_task_id() -> str | None:
    """Return the ECS task ID for metric dimensions, or ``None``.

    Layer 11 may set ``ECS_TASK_ID`` explicitly via the task
    definition. v2 doesn't fetch from the metadata endpoint at
    metric-emit time — that adds a 200ms lookup to every emit.
    """
    return os.environ.get(_TASK_ID_ENV)


class MetricsEmitter:
    """Background coroutine that PUTs ``ActiveSessions`` to CloudWatch.

    Lifecycle: :meth:`start` → coroutine running → :meth:`stop` on
    shutdown. The coroutine self-cancels on
    :exc:`asyncio.CancelledError`; other exceptions log + continue
    so a transient CloudWatch outage doesn't kill the emitter.
    """

    def __init__(self, manager: PipelineManager, settings: Settings) -> None:
        self._manager = manager
        self._settings = settings
        self._task: asyncio.Task | None = None
        # Boto3 client construction is fast; we don't need lazy-init
        # here since the emitter runs on every process start.
        self._client = boto3.client("cloudwatch", region_name=settings.aws_region)
        self._task_id = _resolve_task_id()

    async def start(self) -> None:
        """Start the emit loop. Idempotent — duplicate calls are no-ops."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="metrics-emitter")
        logger.info(
            "metrics_emitter_started",
            namespace=_METRIC_NAMESPACE,
            interval_secs=_EMIT_INTERVAL_SECS,
            task_id=self._task_id,
        )

    async def stop(self) -> None:
        """Cancel the emit loop and await its exit."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("metrics_emitter_stopped")

    async def _loop(self) -> None:
        """Sleep + emit forever (until cancelled)."""
        while True:
            try:
                await asyncio.sleep(_EMIT_INTERVAL_SECS)
                await self._emit()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 — log + continue
                logger.error(
                    "metrics_emit_error",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    async def _emit(self) -> None:
        """One CloudWatch PUT with both metrics."""
        count = self._manager.active_session_count
        max_concurrent = self._manager.get_status()["max_concurrent"] or 1
        utilization_pct = (count / max_concurrent) * 100.0

        dimensions = []
        if self._task_id:
            dimensions = [{"Name": "TaskId", "Value": self._task_id}]

        metric_data = [
            {
                "MetricName": "ActiveSessions",
                "Value": float(count),
                "Unit": "Count",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "SessionUtilization",
                "Value": utilization_pct,
                "Unit": "Percent",
                "Dimensions": dimensions,
            },
        ]

        await asyncio.to_thread(
            self._client.put_metric_data,
            Namespace=_METRIC_NAMESPACE,
            MetricData=metric_data,
        )
        logger.debug(
            "metrics_emitted",
            active_sessions=count,
            utilization_pct=round(utilization_pct, 1),
        )
