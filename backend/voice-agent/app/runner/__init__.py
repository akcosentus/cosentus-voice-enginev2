"""Layer 9 — HTTP + trigger handler / process runner.

Composes Layers 1-8 into a production-deployable engine:

* :class:`PipelineManager` — per-process call lifecycle. Owns the
  ``active_sessions`` dict, capacity gating, draining flag, and
  the dict-boundary lifecycle that triggers ECS task scale-in
  protection.
* :class:`TaskProtection` — ECS agent endpoint client. Acquires
  protection when ``active_sessions`` transitions 0→1; releases on
  1→0. Heartbeat coroutine renews every 30 s. Ported verbatim
  from v1.
* :class:`DailyRoomClient` — Daily REST API client. Three room
  shapes (inbound SIP, outbound dialout, browser WebRTC) plus
  meeting-token minting.
* :func:`build_app` — aiohttp application factory. Registers the
  five HTTP routes (``/health``, ``/ready``, ``/status``,
  ``/start``, ``/daily-dialin-webhook``).
* :func:`graceful_drain` — SIGTERM-driven drain coroutine. Sets
  the draining flag, polls ``active_sessions`` for up to 110 s,
  cancels survivors (only OUR tasks — not all asyncio tasks),
  releases protection.
* :class:`MetricsEmitter` — CloudWatch metric emission for
  auto-scaling. ``ActiveSessions`` + ``SessionUtilization`` every
  30 s.

Layer 8's :func:`~app.bot.bot` is imported by :class:`PipelineManager`
and called via ``asyncio.create_task`` per spawn. The bot file
itself is not modified.
"""

from app.runner.daily_rooms import DailyAPIError, DailyRoom, DailyRoomClient
from app.runner.manager import CapacityRejected, PipelineManager
from app.runner.metrics import MetricsEmitter
from app.runner.protection import TaskProtection
from app.runner.server import build_app, graceful_drain

__all__ = [
    "CapacityRejected",
    "DailyAPIError",
    "DailyRoom",
    "DailyRoomClient",
    "MetricsEmitter",
    "PipelineManager",
    "TaskProtection",
    "build_app",
    "graceful_drain",
]
