"""Per-process call lifecycle manager.

Owns ``active_sessions`` (the dict of in-flight asyncio tasks),
capacity gating, draining flag, and the dict-boundary lifecycle
that triggers ECS task scale-in protection.

Layer 9 split rationale (vs v1's single ``PipelineManager``):

v1's ``PipelineManager`` mixed 10 concerns. v2 splits them across
layers. This manager keeps only:

* Active session dict + lifecycle (spawn → cleanup).
* Capacity / draining gates.
* Protection 0↔1 boundary triggers + heartbeat coroutine.

What v2 dropped (lives elsewhere now):

* Per-call agent loading + hydration → Layer 8's ``run_bot``.
* Per-call ``CallRecord`` write + PCA + auto-actions →
  Layer 6 + Layer 8's ``finalize_call``.
* Per-call collector creation → Layer 7's accumulator + state.
* Per-call structlog contextvars binding → Layer 8's ``run_bot``.
* DynamoDB session tracker → Layer 11.
* EMF logger → :class:`~app.runner.metrics.MetricsEmitter`.
* HTTP-shaped capacity rejection (``http_status`` field) →
  :class:`CapacityRejected` exception caught by the route handler.

The manager is per-process (one instance shared across all
concurrent calls). Layer 9's :func:`build_app` constructs it once
at startup and stores on ``app["manager"]``.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from pipecat.runner.types import DailyRunnerArguments

from app.bot import bot
from app.config.settings import Settings
from app.runner.daily_rooms import DailyRoomClient
from app.runner.protection import TaskProtection

logger = structlog.get_logger(__name__)

# Heartbeat cadence for ECS task protection renewal. 30 s is well
# under the 30-minute ``ExpiresInMinutes`` so we have plenty of
# margin for transient failures (3 retries × backoff ≈ <1 s) and
# for the call-end coalescing window.
_HEARTBEAT_INTERVAL_SECS = 30


@dataclass(frozen=True)
class CallSpawnResult:
    """Returned by ``start_*`` methods. Layer 9's HTTP route handlers
    serialize these into 202 responses.

    Attributes:
        call_id: Engine-generated UUID. The Layer-8 bot generates
            its own ``call_id`` internally; this manager's
            ``call_id`` is a separate identifier used as the dict
            key in ``active_sessions``.
        room_name: Daily room name (= ``CallRecord.session_id``).
        room_url: Full Daily room URL.
        viewer_token: Browser-only. Test clients use this to join
            the room as a non-owner participant.
    """

    call_id: str
    room_name: str
    room_url: str
    viewer_token: str | None = None


class CapacityRejected(Exception):
    """Raised by ``start_*`` when the call cannot be accepted.

    Two reasons (in :attr:`reason`):

    * ``"draining"`` — process received SIGTERM; not accepting
      new work.
    * ``"at_capacity"`` — already at ``max_concurrent_calls``.

    HTTP route handlers catch this and return 503.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PipelineManager:
    """Per-process call lifecycle manager.

    Single instance shared across the asyncio event loop. All
    public methods are coroutines and safe to call concurrently —
    the dict-boundary checks happen in await-free regions, so
    asyncio's single-threaded scheduling guarantees the 0↔1
    transitions fire exactly once per actual transition.
    """

    def __init__(
        self,
        settings: Settings,
        daily_client: DailyRoomClient,
        protection: TaskProtection,
    ) -> None:
        self._settings = settings
        self._daily = daily_client
        self._protection = protection
        self._active_sessions: dict[str, asyncio.Task] = {}
        self._draining = False
        self._max_concurrent = settings.max_concurrent_calls
        self._heartbeat_task: asyncio.Task | None = None

    # ── Status accessors ──────────────────────────────────────────

    @property
    def is_draining(self) -> bool:
        return self._draining

    @property
    def active_session_count(self) -> int:
        return len(self._active_sessions)

    @property
    def at_capacity(self) -> bool:
        return len(self._active_sessions) >= self._max_concurrent

    @property
    def active_sessions(self) -> dict[str, asyncio.Task]:
        """Read-only-by-convention dict view. Callers MUST NOT mutate.

        Used by :func:`graceful_drain` to iterate and cancel the
        engine's spawned tasks (and only those tasks — not all
        ``asyncio.all_tasks``, which would kill the HTTP server's
        accept loop along with everything else).
        """
        return self._active_sessions

    def get_status(self) -> dict[str, Any]:
        """Public status snapshot for ``/status`` and ``/ready``."""
        return {
            "active_sessions": self.active_session_count,
            "max_concurrent": self._max_concurrent,
            "draining": self._draining,
            "protected": self._protection.is_protected,
            "protection_available": self._protection.is_available,
        }

    # ── Spawn entry points ────────────────────────────────────────

    async def start_outbound(
        self,
        *,
        agent_id: str,
        target_number: str,
        from_number: str,
        case_data: dict[str, Any] | None = None,
        batch_id: str | None = None,
        batch_row_index: int | None = None,
    ) -> CallSpawnResult:
        """Outbound PSTN call. Creates a dialout-enabled Daily room,
        mints a bot token, spawns the bot, and returns the spawn
        result. The bot itself dials out from ``on_joined``.
        """
        self._reject_if_unavailable()

        room = await self._daily.create_outbound_room()
        token = await self._daily.mint_token(room.name)

        call_id = str(uuid.uuid4())
        runner_args = DailyRunnerArguments(
            room_url=room.url,
            token=token,
            body={
                "agent_id": agent_id,
                "direction": "outbound",
                "target_number": target_number,
                "from_number": from_number,
                "case_data": case_data or {},
                "batch_id": batch_id,
                "batch_row_index": batch_row_index,
                # Daily SDK key naming: camelCase. Layer 8's
                # ``on_joined`` handler passes this verbatim to
                # ``transport.start_dialout``.
                "dialout_settings": {
                    "phoneNumber": target_number,
                    "callerId": from_number,
                },
            },
        )

        await self._spawn(call_id, runner_args)

        return CallSpawnResult(
            call_id=call_id,
            room_name=room.name,
            room_url=room.url,
        )

    async def start_browser(
        self,
        *,
        agent_id: str,
        case_data: dict[str, Any] | None = None,
    ) -> CallSpawnResult:
        """Browser test call. Creates a WebRTC-only room, mints both
        a bot token and a viewer token, spawns the bot. The dashboard
        / Cindy widget joins with the viewer token.
        """
        self._reject_if_unavailable()

        room = await self._daily.create_browser_room()
        bot_token = await self._daily.mint_token(room.name, is_owner=True)
        # Viewer token is non-owner; matches the room's 15-min TTL
        # so we don't issue a long-lived token to a short-lived room.
        viewer_token = await self._daily.mint_token(
            room.name,
            is_owner=False,
            exp_secs=900,
        )

        call_id = str(uuid.uuid4())
        runner_args = DailyRunnerArguments(
            room_url=room.url,
            token=bot_token,
            body={
                "agent_id": agent_id,
                "direction": "browser",
                "target_number": "",
                "from_number": "",
                "case_data": case_data or {},
            },
        )

        await self._spawn(call_id, runner_args)

        return CallSpawnResult(
            call_id=call_id,
            room_name=room.name,
            room_url=room.url,
            viewer_token=viewer_token,
        )

    async def start_inbound(
        self,
        *,
        agent_id: str,
        from_number: str,
        to_number: str,
        call_id_external: str,
        call_domain: str,
    ) -> CallSpawnResult:
        """Inbound PSTN call. Creates a SIP-dial-in-enabled room,
        mints a bot token, spawns the bot. The dialin webhook
        handler returns the room's ``sip_uri`` to Daily so the
        caller is bridged into the room.

        The ``call_id_external`` and ``call_domain`` come from
        Daily's webhook payload and are passed through to the bot's
        ``dialin_settings`` so :class:`DailyTransport` can correlate
        the SIP leg.
        """
        self._reject_if_unavailable()

        room = await self._daily.create_inbound_room()
        token = await self._daily.mint_token(room.name)

        call_id = str(uuid.uuid4())
        runner_args = DailyRunnerArguments(
            room_url=room.url,
            token=token,
            body={
                "agent_id": agent_id,
                "direction": "inbound",
                "target_number": to_number,
                "from_number": from_number,
                "case_data": {},
                "dialin_settings": {
                    "call_id": call_id_external,
                    "call_domain": call_domain,
                },
            },
        )

        await self._spawn(call_id, runner_args)

        return CallSpawnResult(
            call_id=call_id,
            room_name=room.name,
            room_url=room.url,
        )

    # ── Internals ────────────────────────────────────────────────

    def _reject_if_unavailable(self) -> None:
        """Synchronous gate at the top of every ``start_*``."""
        if self._draining:
            raise CapacityRejected("draining")
        if self.at_capacity:
            raise CapacityRejected("at_capacity")

    async def _spawn(self, call_id: str, runner_args: DailyRunnerArguments) -> None:
        """Create the asyncio task and register it. Acquire protection
        on the 0→1 boundary.

        The boundary check + dict insert happen in the same
        await-free region. Within asyncio's single-threaded loop,
        no other coroutine can interleave — protection is acquired
        exactly once for the first call and released exactly once
        when the last call finishes.
        """
        was_empty = len(self._active_sessions) == 0

        task = asyncio.create_task(
            self._wrapped_bot(call_id, runner_args),
            name=f"call-{call_id}",
        )
        self._active_sessions[call_id] = task

        if was_empty:
            # First call — acquire protection. ``set_protected``
            # has its own retry policy; if it fails after retries
            # we still proceed (the call goes through; we just
            # might get scaled-in if Fargate decides to).
            await self._protection.set_protected(True)
            # Start heartbeat coroutine if not already running.
            if self._heartbeat_task is None or self._heartbeat_task.done():
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(),
                    name="task-protection-heartbeat",
                )

        logger.info(
            "call_spawned",
            call_id=call_id,
            active_sessions=len(self._active_sessions),
            max_concurrent=self._max_concurrent,
        )

    async def _wrapped_bot(self, call_id: str, runner_args: DailyRunnerArguments) -> None:
        """Wrap Layer 8's :func:`~app.bot.bot` to handle dict cleanup.

        Pop happens in a ``finally`` so it fires on success, on
        exception, and on :exc:`asyncio.CancelledError`. The 1→0
        boundary check then fires the protection release.

        The bot itself never raises out (it captures everything
        into ``CallRecord.error`` via Layer 8's finally block). This
        wrapper is defensive — if the bot somehow does raise, we
        still clean up the dict so capacity isn't permanently
        consumed.
        """
        try:
            await bot(runner_args)
        finally:
            self._active_sessions.pop(call_id, None)
            if len(self._active_sessions) == 0 and self._protection.is_protected:
                await self._protection.set_protected(False)
            logger.info(
                "call_finalized",
                call_id=call_id,
                active_sessions=len(self._active_sessions),
            )

    async def _heartbeat_loop(self) -> None:
        """Renew task protection every 30 s while sessions are active.

        Stops itself when ``active_sessions`` empties. The next
        ``_spawn`` 0→1 transition restarts a fresh task.

        Cancellation (process shutdown) is silent. Other exceptions
        log + continue — a bad heartbeat shouldn't kill protection
        permanently.
        """
        try:
            while len(self._active_sessions) > 0:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_SECS)
                if len(self._active_sessions) > 0:
                    await self._protection.renew_if_protected()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "heartbeat_loop_error",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    async def shutdown(self) -> None:
        """Mark the manager as draining. Called from
        :func:`~app.runner.server.graceful_drain` on SIGTERM.

        After this returns, ``/ready`` returns 503 and ``start_*``
        raises :exc:`CapacityRejected("draining")`.
        """
        self._draining = True
        logger.info(
            "manager_shutdown_initiated",
            active_sessions=self.active_session_count,
        )
