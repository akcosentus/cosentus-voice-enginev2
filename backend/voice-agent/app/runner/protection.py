"""ECS task scale-in protection client.

Ported from v1's ``backend/voice-agent/app/task_protection.py``
(MIT-0 source: ``aws-solutions-library-samples/sample-voice-agent``).
The behavior is correct, retry-hardened, and battle-tested in
production; v2 reuses it verbatim with only import-path adjustments.

Lifecycle pattern (verified against AWS docs)
---------------------------------------------

The container talks to ``$ECS_AGENT_URI/task-protection/v1/state``,
the local ECS-agent endpoint that ECS injects into the task. PUT
sets protection; GET reads it. v2 only PUTs.

Protection lifecycle is **dict-boundary driven**:

* When ``PipelineManager.active_sessions`` transitions 0 → 1: PUT
  ``ProtectionEnabled=true``.
* When ``active_sessions`` transitions 1 → 0: PUT
  ``ProtectionEnabled=false``.

Individual calls don't touch protection — no per-call API calls.
Race-safety: ``pop()`` and ``len()`` checks are synchronous in the
asyncio event loop, so no interleaving is possible inside a single
coroutine. Layer 9's :class:`PipelineManager` does the boundary
check and the dict mutation in the same await-free block.

Heartbeat renewal
-----------------

Protection expires after ``PROTECTION_EXPIRY_MINUTES`` (30 by
default). A background coroutine in :class:`PipelineManager`
renews every 30 s while ``active_sessions`` is non-empty. If three
consecutive renewals fail (network blip, ECS agent restart), the
escalation threshold logs an ERROR so operations gets paged.

Reference: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-scale-in-protection-endpoint.html
"""

from __future__ import annotations

import asyncio
import os

import aiohttp
import structlog

logger = structlog.get_logger(__name__)


# Protection expires after this many minutes if not renewed. 30 min
# is a safety net for stuck sessions; the heartbeat in
# :class:`PipelineManager` renews every 30 s so this never trips
# in practice. Bounded between 1 and 2880 (48 h) per ECS docs.
PROTECTION_EXPIRY_MINUTES = 30

# Retry policy for both initial set and renewal. 3 attempts with
# 100 ms / 200 ms / 400 ms exponential backoff. Total worst-case
# delay ~700 ms on top of the 5 s timeout — manageable for the
# critical 0→1 transition.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 0.1  # 100 ms

# Total HTTP timeout for any one request. ECS agent endpoint is
# local, so 5 s is generous.
REQUEST_TIMEOUT_SECS = 5

# After this many consecutive renewal failures we escalate logging
# from WARNING to ERROR. Operations alarm fires on the ERROR.
RENEWAL_ESCALATION_THRESHOLD = 3


class TaskProtection:
    """Manages ECS task scale-in protection for voice call handling.

    Single reusable :class:`aiohttp.ClientSession` keeps a connection
    pool to the local ECS-agent API across acquire / renew / release
    calls. The session is lazy-initialized on first use and closed
    via :meth:`close` during shutdown.

    Local development (no ``ECS_AGENT_URI``) is a graceful no-op:
    :attr:`is_available` returns ``False`` and every method logs +
    returns ``False`` without raising. Tests don't need ECS agent
    access.
    """

    def __init__(self) -> None:
        self._agent_uri = os.environ.get("ECS_AGENT_URI")
        self._protected = False
        self._session: aiohttp.ClientSession | None = None
        self._consecutive_renewal_failures = 0

    @property
    def is_available(self) -> bool:
        """``True`` when the ECS agent endpoint is reachable.

        ``False`` in local development (no ``ECS_AGENT_URI`` env var).
        """
        return self._agent_uri is not None

    @property
    def is_protected(self) -> bool:
        """Whether scale-in protection is currently asserted on this task."""
        return self._protected

    async def _get_session(self) -> aiohttp.ClientSession:
        """Lazy-init the shared HTTP session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECS),
            )
        return self._session

    async def set_protected(self, protected: bool, retry: bool = True) -> bool:
        """PUT the protection state to the ECS agent.

        Args:
            protected: ``True`` to acquire protection (typically on
                the 0→1 active-sessions boundary), ``False`` to
                release (1→0 boundary).
            retry: When ``True`` (default), retry up to
                :data:`MAX_RETRIES` times with exponential backoff
                on transient failure.

        Returns:
            ``True`` when the PUT succeeded (200 status).
            ``False`` when the endpoint is unavailable (local dev),
            the state already matches, or every retry exhausted.
        """
        if not self.is_available:
            logger.debug("task_protection_unavailable", reason="local_dev")
            return False

        if protected == self._protected:
            logger.debug(
                "task_protection_no_change",
                protection_enabled=protected,
            )
            return True

        endpoint = f"{self._agent_uri}/task-protection/v1/state"
        payload: dict = {"ProtectionEnabled": protected}
        if protected:
            payload["ExpiresInMinutes"] = PROTECTION_EXPIRY_MINUTES

        max_attempts = MAX_RETRIES if retry else 1
        for attempt in range(max_attempts):
            try:
                session = await self._get_session()
                async with session.put(endpoint, json=payload) as resp:
                    if resp.status == 200:
                        self._protected = protected
                        logger.info(
                            "task_protection_updated",
                            protection_enabled=protected,
                        )
                        return True
                    body = await resp.text()
                    logger.error(
                        "task_protection_api_error",
                        status=resp.status,
                        body=body[:200],
                        attempt=attempt + 1,
                    )
            except Exception:  # noqa: BLE001 — log + retry, don't propagate
                logger.exception(
                    "task_protection_api_exception",
                    attempt=attempt + 1,
                )

            if attempt < max_attempts - 1:
                delay = RETRY_BASE_DELAY * (2**attempt)
                await asyncio.sleep(delay)

        logger.error(
            "task_protection_all_retries_exhausted",
            protected=protected,
            attempts=max_attempts,
        )
        return False

    async def renew_if_protected(self) -> bool:
        """Renew protection if currently asserted.

        Called from :class:`PipelineManager`'s heartbeat loop every
        30 s while active sessions exist. Resets the
        ``ExpiresInMinutes`` timer; ensures protection never lapses
        mid-call.

        Tracks consecutive failures. After
        :data:`RENEWAL_ESCALATION_THRESHOLD` consecutive misses,
        upgrades the log severity from WARNING to ERROR so
        operations alerts fire. The next successful renewal logs a
        ``task_protection_renewal_recovered`` event with the prior
        failure count.

        Returns:
            ``True`` on successful renewal, ``False`` when not
            currently protected, when the endpoint is unavailable,
            or when every retry exhausted.
        """
        if not self.is_available or not self._protected:
            return False

        endpoint = f"{self._agent_uri}/task-protection/v1/state"
        payload = {
            "ProtectionEnabled": True,
            "ExpiresInMinutes": PROTECTION_EXPIRY_MINUTES,
        }

        for attempt in range(MAX_RETRIES):
            try:
                session = await self._get_session()
                async with session.put(endpoint, json=payload) as resp:
                    if resp.status == 200:
                        if self._consecutive_renewal_failures > 0:
                            logger.info(
                                "task_protection_renewal_recovered",
                                previous_failures=self._consecutive_renewal_failures,
                            )
                        self._consecutive_renewal_failures = 0
                        logger.debug("task_protection_renewed")
                        return True
                    body = await resp.text()
                    logger.warning(
                        "task_protection_renewal_failed",
                        status=resp.status,
                        body=body[:200],
                        attempt=attempt + 1,
                    )
            except Exception as exc:  # noqa: BLE001 — log + retry
                logger.warning(
                    "task_protection_renewal_exception",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    attempt=attempt + 1,
                )

            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BASE_DELAY * (2**attempt)
                await asyncio.sleep(delay)

        # All retries exhausted.
        self._consecutive_renewal_failures += 1
        if self._consecutive_renewal_failures >= RENEWAL_ESCALATION_THRESHOLD:
            logger.error(
                "task_protection_renewal_persistent_failure",
                consecutive_failures=self._consecutive_renewal_failures,
                attempts=MAX_RETRIES,
                note="Protection may lapse if failures continue",
            )
        else:
            logger.warning(
                "task_protection_renewal_all_retries_exhausted",
                consecutive_failures=self._consecutive_renewal_failures,
                attempts=MAX_RETRIES,
            )
        return False

    async def close(self) -> None:
        """Close the shared HTTP session. Call during shutdown."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
