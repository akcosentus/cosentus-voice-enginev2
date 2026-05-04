"""HTTP server (aiohttp) + graceful drain.

Five routes, all per-process:

* ``GET /health`` — liveness probe. Always 200 unless the process
  is broken. ECS uses this to decide whether to restart the
  container.
* ``GET /ready`` — readiness probe. 503 when draining or at
  capacity; 200 otherwise. NLB uses this to decide whether to
  route new calls to this task.
* ``GET /status`` — detailed manager state. Auth required (the
  output reveals operational details).
* ``POST /start`` — outbound or browser call spawn. Auth required.
  ``body.direction`` switches between the two.
* ``POST /daily-dialin-webhook`` — inbound PSTN call from Daily's
  SIP gateway. NO auth (Daily-signed; signature verification is
  TODO once SIP is configured).

:func:`graceful_drain` is the SIGTERM-driven shutdown coroutine.
Sets the manager's draining flag, polls active sessions for up to
110 seconds, cancels survivors (only the engine's spawned tasks —
not all asyncio tasks, which would kill the HTTP accept loop), and
releases ECS task protection.

aiohttp choice rationale (vs FastAPI): single event loop, lower
overhead, v1 + AWS sample production-proven, no FastAPI →
starlette → uvicorn dependency chain for what's effectively five
endpoints. ``fastapi`` is already a transitive dep (Layer 8 needs
it for ``pipecat.runner.types``) but we don't import it directly
here.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import boto3
import structlog
from aiohttp import web

from app.config.settings import Settings
from app.runner.manager import CapacityRejected, PipelineManager
from app.runner.protection import TaskProtection

# Typed AppKey instances. aiohttp 3.9+ recommends these for app[]
# storage to avoid the ``NotAppKeyWarning`` and to give type-checkers
# something to bite. Each key is module-scoped so route handlers
# import them implicitly via the same module.
APP_KEY_MANAGER: web.AppKey[PipelineManager] = web.AppKey("manager", PipelineManager)
APP_KEY_SETTINGS: web.AppKey[Settings] = web.AppKey("settings", Settings)
APP_KEY_API_KEY: web.AppKey[str] = web.AppKey("api_key", str)

logger = structlog.get_logger(__name__)


# Drain budget in seconds. Fargate's default ``stopTimeout`` is
# 120 s before SIGKILL; we leave a 10 s buffer for the post-drain
# protection release + session cleanup. Layer 11 may bump
# ``stopTimeout`` to 600 s for longer Cosentus IVR sessions; this
# default tracks the conservative 120 s case.
DEFAULT_DRAIN_BUDGET_SECS = 110

# Polling interval inside the drain coroutine.
_DRAIN_POLL_SECS = 5

# Window after cancelling survivor tasks for their finally blocks
# to fire (Layer 8's ``finalize_call`` must run to land a partial
# CallRecord). 2 s covers normal shutdown; longer would risk
# Fargate killing us before the drain returns.
_POST_CANCEL_GRACE_SECS = 2


# ── App factory ────────────────────────────────────────────────────────────


async def build_app(settings: Settings, manager: PipelineManager) -> web.Application:
    """Construct the aiohttp application and wire routes.

    Stores ``manager``, ``settings``, and the resolved API key on
    ``app[...]`` so route handlers can reach them via the request's
    app reference. No globals.
    """
    app = web.Application()
    app[APP_KEY_MANAGER] = manager
    app[APP_KEY_SETTINGS] = settings
    app[APP_KEY_API_KEY] = await _load_api_key(settings)

    app.router.add_get("/health", handle_health)
    app.router.add_get("/ready", handle_ready)
    app.router.add_get("/status", handle_status)
    app.router.add_post("/start", handle_start)
    app.router.add_post("/daily-dialin-webhook", handle_dialin_webhook)

    logger.info(
        "app_built",
        routes=["/health", "/ready", "/status", "/start", "/daily-dialin-webhook"],
        api_key_configured=bool(app[APP_KEY_API_KEY]),
    )
    return app


# ── Route handlers ────────────────────────────────────────────────────────


async def handle_health(request: web.Request) -> web.Response:
    """Liveness. Returns 200 while the process is running."""
    return web.json_response({"status": "healthy"})


async def handle_ready(request: web.Request) -> web.Response:
    """Readiness. 503 when draining or at capacity; 200 otherwise.

    Separate from ``/health`` so ECS doesn't kill at-capacity
    containers. NLB stops routing when this returns 503; ECS keeps
    the task running for in-progress calls.
    """
    manager: PipelineManager = request.app[APP_KEY_MANAGER]
    if manager.is_draining:
        return web.json_response(
            {"status": "draining", **manager.get_status()},
            status=503,
        )
    if manager.at_capacity:
        return web.json_response(
            {"status": "at_capacity", **manager.get_status()},
            status=503,
        )
    return web.json_response({"status": "ready", **manager.get_status()})


async def handle_status(request: web.Request) -> web.Response:
    """Detailed status. Auth required — output reveals operational state."""
    if not _check_api_key(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    manager: PipelineManager = request.app[APP_KEY_MANAGER]
    return web.json_response(manager.get_status())


async def handle_start(request: web.Request) -> web.Response:
    """Spawn an outbound or browser call. Auth required.

    Returns 202 Accepted immediately after ``asyncio.create_task``.
    The caller polls ``/status`` if it needs to know whether the
    call is live; the engine doesn't wait for the bot to join Daily
    before responding.
    """
    if not _check_api_key(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    manager: PipelineManager = request.app[APP_KEY_MANAGER]

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    direction = body.get("direction", "outbound")
    agent_id = body.get("agent_id")
    if not agent_id:
        return web.json_response(
            {"error": "agent_id_required"},
            status=400,
        )

    try:
        if direction == "outbound":
            target_number = body.get("target_number")
            from_number = body.get("from_number")
            if not target_number or not from_number:
                return web.json_response(
                    {"error": "target_number_and_from_number_required"},
                    status=400,
                )
            result = await manager.start_outbound(
                agent_id=agent_id,
                target_number=target_number,
                from_number=from_number,
                case_data=body.get("case_data") or {},
                batch_id=body.get("batch_id"),
                batch_row_index=body.get("batch_row_index"),
            )
        elif direction == "browser":
            result = await manager.start_browser(
                agent_id=agent_id,
                case_data=body.get("case_data"),
            )
        else:
            return web.json_response(
                {"error": f"unknown_direction:{direction}"},
                status=400,
            )
    except CapacityRejected as exc:
        return web.json_response(
            {"status": "rejected", "reason": exc.reason},
            status=503,
        )

    response: dict[str, Any] = {
        "call_id": result.call_id,
        "room_name": result.room_name,
        "room_url": result.room_url,
        "status": "started",
    }
    if result.viewer_token is not None:
        response["viewer_token"] = result.viewer_token

    return web.json_response(response, status=202)


async def handle_dialin_webhook(request: web.Request) -> web.Response:
    """Daily dial-in webhook. NO auth (Daily-signed).

    TODO: verify Daily's webhook signature once SIP is configured
    in production. Until then, the network-layer security
    (Application Load Balancer source-IP allow-list to Daily's
    egress range) is the defense.

    Returns the room URL and session ID Daily expects in its
    documented response shape — Daily's SIP gateway uses this to
    bridge the caller into the room.
    """
    manager: PipelineManager = request.app[APP_KEY_MANAGER]
    settings: Settings = request.app[APP_KEY_SETTINGS]

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    from_number = body.get("From", "")
    to_number = body.get("To", "")
    call_id_external = body.get("callId", "")
    call_domain = body.get("callDomain", "")

    if not to_number:
        return web.json_response({"error": "to_required"}, status=400)

    # Phone-number → agent_id lookup via the API lambda.
    agent_id = await _lookup_inbound_agent(to_number, settings)
    if not agent_id:
        logger.warning(
            "dialin_no_agent_configured",
            to_number=to_number,
            from_number=from_number,
        )
        return web.json_response(
            {"error": "no_agent_configured", "to": to_number},
            status=503,
        )

    try:
        result = await manager.start_inbound(
            agent_id=agent_id,
            from_number=from_number,
            to_number=to_number,
            call_id_external=call_id_external,
            call_domain=call_domain,
        )
    except CapacityRejected as exc:
        return web.json_response(
            {"error": "at_capacity", "reason": exc.reason},
            status=503,
        )

    return web.json_response(
        {
            "dailyRoom": result.room_url,
            "sessionId": result.call_id,
        }
    )


# ── Auth helpers ──────────────────────────────────────────────────────────


def _check_api_key(request: web.Request) -> bool:
    """Constant-time-ish API key check. Returns ``False`` to short-circuit.

    Empty configured key (local dev) means no auth — return ``True``
    so dev environments don't have to set the header. Production
    sets ``api_key_secret_arn``, the resolved secret is non-empty,
    and the header check fires.
    """
    expected = request.app.get(APP_KEY_API_KEY, "")
    if not expected:
        return True  # local dev — no auth
    provided = request.headers.get("X-API-Key", "")
    return provided == expected


async def _load_api_key(settings: Settings) -> str:
    """Resolve the API key from Secrets Manager.

    Empty-string ``api_key_secret_arn`` (the local-dev case) skips
    the AWS call entirely and returns ``""``, which
    :func:`_check_api_key` interprets as "auth disabled."

    The secret blob is treated as a JSON object with an ``api_key``
    field; a plain-string secret is also accepted. Production sets
    a JSON secret so other Cosentus services can extend with
    additional fields without rotating the secret name.
    """
    arn = settings.api_key_secret_arn
    if not arn:
        return ""
    client = boto3.client("secretsmanager", region_name=settings.aws_region)
    try:
        response = await asyncio.to_thread(
            client.get_secret_value,
            SecretId=arn,
        )
    except Exception as exc:  # noqa: BLE001 — log + continue (no-auth dev path)
        logger.error(
            "api_key_secret_load_failed",
            arn=arn,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return ""

    raw = response.get("SecretString", "") or ""
    # Try JSON first, fall back to raw string.
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return str(parsed.get("api_key") or parsed.get("apiKey") or "")
    except (ValueError, json.JSONDecodeError):
        pass
    return raw


# ── Phone-number lookup (inbound agent_id resolution) ────────────────────


async def _lookup_inbound_agent(to_number: str, settings: Settings) -> str | None:
    """Resolve ``to_number`` → ``inbound_agent_id`` via the API lambda.

    Reuses Layer 1's hardened ``_get_lambda_client(settings)`` for
    timeout + adaptive retry. Returns ``None`` on any failure path
    (lookup 404, lambda invoke error, malformed response). The
    caller (dialin webhook handler) maps ``None`` to a 503 so Daily
    retries — temporary issues self-heal; permanent issues page
    operations.
    """
    from app.config.agent_config import _get_lambda_client

    client = _get_lambda_client(settings)
    envelope = {
        "httpMethod": "GET",
        "path": "/api/phone-numbers/lookup",
        "queryStringParameters": {"number": to_number},
        "headers": {},
        "body": None,
    }
    try:
        response = await asyncio.to_thread(
            client.invoke,
            FunctionName=settings.voice_api_lambda_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(envelope).encode("utf-8"),
        )
    except Exception as exc:  # noqa: BLE001 — log + return None
        logger.error(
            "phone_lookup_invoke_failed",
            to_number=to_number,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None

    try:
        payload_bytes = response["Payload"].read()
        payload = json.loads(payload_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "phone_lookup_bad_envelope",
            to_number=to_number,
            error=str(exc),
        )
        return None

    if payload.get("statusCode", 0) != 200:
        logger.warning(
            "phone_lookup_non_200",
            to_number=to_number,
            status_code=payload.get("statusCode"),
        )
        return None

    body_text = payload.get("body", "{}") or "{}"
    try:
        data = json.loads(body_text)
    except (ValueError, json.JSONDecodeError):
        return None
    return data.get("inbound_agent_id")


# ── Graceful drain ────────────────────────────────────────────────────────


async def graceful_drain(
    manager: PipelineManager,
    protection: TaskProtection,
    *,
    budget_secs: int = DEFAULT_DRAIN_BUDGET_SECS,
) -> None:
    """SIGTERM-driven drain. Set draining flag, wait for active calls,
    cancel survivors, release protection.

    Cancels only the manager's spawned tasks
    (``manager.active_sessions.values()``), NOT every task on the
    asyncio loop. v1's ``for task in asyncio.all_tasks(loop)`` was
    a sledgehammer that killed the HTTP server's accept loop too —
    fine in practice (the process was about to exit anyway) but
    pollutes logs with bogus cancellations and races against the
    web.AppRunner's own cleanup. v2 ships fixed.

    The ``budget_secs`` matches Fargate's default 120 s
    ``stopTimeout`` minus a 10 s buffer for the post-drain
    protection release + cleanup work. Layer 11 may bump Fargate's
    stopTimeout for longer Cosentus IVR calls; pass a matching
    larger ``budget_secs`` here.
    """
    logger.info(
        "drain_starting",
        active_sessions=manager.active_session_count,
        budget_secs=budget_secs,
    )
    await manager.shutdown()  # sets draining flag; future /ready returns 503

    drain_start = time.time()
    while manager.active_session_count > 0:
        elapsed = time.time() - drain_start
        if elapsed > budget_secs:
            logger.warning(
                "drain_timeout",
                remaining=manager.active_session_count,
                elapsed_secs=round(elapsed, 1),
            )
            # Cancel only the engine's tasks — NOT asyncio.all_tasks.
            for task in list(manager.active_sessions.values()):
                task.cancel()
            # Brief grace window for finally blocks to fire
            # (Layer 8's finalize_call writes a CallRecord even on
            # cancel).
            await asyncio.sleep(_POST_CANCEL_GRACE_SECS)
            break
        logger.info(
            "drain_waiting",
            remaining=manager.active_session_count,
            elapsed_secs=round(elapsed, 1),
        )
        await asyncio.sleep(_DRAIN_POLL_SECS)

    # Release protection so ECS can scale this task in.
    await protection.set_protected(False)
    await protection.close()
    logger.info("drain_complete", duration_secs=round(time.time() - drain_start, 1))
