"""CloudWatch metric emission for ECS auto-scaling.

Emits two periodic metrics every :data:`_EMIT_INTERVAL_SECS` while
the process runs, plus one ad-hoc counter on drain timeout:

* ``ActiveSessions`` — current ``len(active_sessions)``. Drives the
  target-tracking auto-scaling policy and the
  ``ActiveSessionsApproachingMax`` alarm wired in Layer 11 Wave 3.
* ``SessionUtilization`` — ``active / max_concurrent`` as a
  percentage. Diagnostic; powers the dashboard's per-task panel.
  Not directly wired to an alarm but the metric name is referenced
  in CDK; do not rename without updating
  ``infrastructure/src/constructs/monitoring.ts``.
* ``DrainTimeouts`` — emitted on each graceful-drain timeout
  (server.py's :func:`graceful_drain`). Powers the
  ``DrainTimeoutsAboveThreshold`` alarm wired in Wave 3
  (``Sum > 0`` over 1 hour). Value is always 1 — each emission is
  one timeout event. The number of stranded calls at the moment
  of timeout is captured in the ``drain_timeout`` log line for
  diagnostics, not in the metric value.

Wire to Wave 3 alarms / scaling policy
--------------------------------------

Renaming any metric or changing its dimensions silently breaks the
CDK side. The current contract:

* Namespace        : ``VoiceAgent/Pipeline``
* Dimensions       : ``Environment`` always; ``TaskId`` when
                     ``ECS_TASK_ID`` env var is set (debug-only;
                     Wave 3 task definitions do not set it, so the
                     production timeseries is keyed by Environment).
* Emit cadence     : :data:`_EMIT_INTERVAL_SECS` (30 s) for the
                     periodic pair; on-demand for ``DrainTimeouts``.

If you change any of the above, update
``infrastructure/src/constructs/ecs-service.ts`` (scaling policy)
and ``infrastructure/src/constructs/monitoring.ts`` (alarms +
dashboard) in the same change.

Error handling
--------------

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
        # Environment dimension separates staging vs prod metric
        # timeseries when both fleets emit to the same AWS account
        # (Wave 3 deploys both into 825269749545). Without this,
        # staging activity would trigger prod alarms.
        self._environment = settings.environment

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

    def _build_dimensions(self) -> list[dict[str, str]]:
        """Standard dimension set: Environment always, TaskId when set.

        Wave 3's CDK alarms and scaling policy filter on
        ``Environment`` only, so emissions without that dimension
        would be invisible to the CDK side. The optional
        ``TaskId`` dimension is debug-only; the Wave 3 task
        definition does not set ``ECS_TASK_ID``, so the production
        timeseries is keyed by Environment alone.
        """
        dims: list[dict[str, str]] = [
            {"Name": "Environment", "Value": self._environment},
        ]
        if self._task_id:
            dims.append({"Name": "TaskId", "Value": self._task_id})
        return dims

    async def _emit(self) -> None:
        """One CloudWatch PUT with both periodic metrics."""
        count = self._manager.active_session_count
        max_concurrent = self._manager.get_status()["max_concurrent"] or 1
        utilization_pct = (count / max_concurrent) * 100.0

        dimensions = self._build_dimensions()

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

    async def emit_drain_timeout(self, remaining: int) -> None:
        """Ad-hoc emission for the graceful-drain timeout counter.

        Called by :func:`app.runner.server.graceful_drain` exactly
        once per timeout event. Best-effort: errors are logged and
        swallowed so a CloudWatch outage cannot prevent drain from
        completing. ``remaining`` is the number of still-active
        sessions at the moment of timeout — captured for the log
        line, not the metric value (the metric counts EVENTS).

        Wave 3 alarm ``DrainTimeoutsAboveThreshold`` fires when
        Sum-over-1h of this metric exceeds 0.
        """
        dimensions = self._build_dimensions()
        try:
            await asyncio.to_thread(
                self._client.put_metric_data,
                Namespace=_METRIC_NAMESPACE,
                MetricData=[
                    {
                        "MetricName": "DrainTimeouts",
                        "Value": 1.0,
                        "Unit": "Count",
                        "Dimensions": dimensions,
                    },
                ],
            )
            logger.info("drain_timeout_metric_emitted", remaining=remaining)
        except Exception as exc:  # noqa: BLE001 — drain must continue
            logger.error(
                "drain_timeout_metric_error",
                error=str(exc),
                error_type=type(exc).__name__,
                remaining=remaining,
            )
