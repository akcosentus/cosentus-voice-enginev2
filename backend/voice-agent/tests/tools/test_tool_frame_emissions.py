"""Tool frame-emission regression guards via ``pipecat.tests.utils.run_test``.

For each of v2's three platform tools (``press_digit``,
``transfer_call``, ``end_call``), wrap the executor in a tiny test
processor and run a real Pipecat pipeline (via ``run_test``)
through it. Inspect the captured downstream + upstream frames and
assert:

* No ``InterruptionTaskFrame`` / ``InterruptionFrame`` /
  ``CancelTaskFrame`` is pushed by the tool. Pushing any of those
  from inside a tool handler is the Bug A anti-pattern that broke
  the function-call lifecycle and stripped tool_use / tool_result
  blocks out of the LLM context.
* For ``end_call`` specifically, an ``EndTaskFrame`` lands in the
  upstream captures — the documented Pipecat way for a tool to
  request graceful shutdown.

Why ``run_test`` rather than ``AsyncMock`` on ``queue_frame``? The
mock-based check would pass even if the wiring between
``ToolContext.queue_frame`` and the real ``PipelineTask.queue_frame``
were broken. ``run_test`` exercises the production frame-flow path,
so a regression here is empirically caught.

Reference: https://reference-server.pipecat.ai/en/stable/api/pipecat.tests.utils.html
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.tools.builtin.end_call import end_call_executor
from app.tools.builtin.press_digit import press_digit_executor
from app.tools.builtin.transfer_call import transfer_call_executor
from app.tools.context import ToolContext
from pipecat.frames.frames import (
    CancelTaskFrame,
    DataFrame,
    EndTaskFrame,
    Frame,
    InterruptionFrame,
    InterruptionTaskFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.tests.utils import run_test

_FORBIDDEN_FRAMES = (InterruptionTaskFrame, InterruptionFrame, CancelTaskFrame)


@dataclass
class _RunToolFrame(DataFrame):
    """Test-only sentinel frame. ``ToolEmissionCapture`` invokes the
    tool executor when this frame arrives downstream.
    """

    arguments: dict | None = None


class ToolEmissionCapture(FrameProcessor):
    """Test processor that, on receiving a ``_RunToolFrame``, invokes a
    tool executor with a ``ToolContext`` whose ``queue_frame`` is
    bound to ``self.push_frame``. Any frame the tool emits flows
    through the surrounding ``run_test`` pipeline so the harness's
    source / sink ``QueuedFrameProcessor``s capture them.

    This mirrors how Layer 8 wires real tool calls in production
    (``ToolContext.queue_frame = task.queue_frame``) — the only
    difference is the frame originates from the middle processor
    rather than from ``PipelineTask`` itself, which is harmless for
    direction-flow assertions.
    """

    def __init__(self, executor, ctx_kwargs: dict):
        super().__init__()
        self._executor = executor
        self._ctx_kwargs = ctx_kwargs
        self.tool_result = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, _RunToolFrame):
            ctx = ToolContext(
                queue_frame=self.push_frame,
                **self._ctx_kwargs,
            )
            self.tool_result = await self._executor(frame.arguments or {}, ctx)
            return
        await self.push_frame(frame, direction)


def _assert_no_forbidden_frames(frames, label: str) -> None:
    for f in frames:
        assert not isinstance(f, _FORBIDDEN_FRAMES), (
            f"forbidden pipeline-control frame {type(f).__name__} in {label} "
            f"captures — Bug A anti-pattern regression."
        )


# ── press_digit ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_press_digit_via_run_test_does_not_emit_pipeline_control_frames():
    """Bug A regression guard. The new press_digit calls
    ``transport.send_dtmf`` directly — no frame pushes — so the
    pipeline captures should contain neither ``InterruptionTaskFrame``
    nor any other pipeline-control frame.
    """
    transport = MagicMock()
    transport.send_dtmf = AsyncMock(return_value=None)

    capture = ToolEmissionCapture(
        press_digit_executor,
        ctx_kwargs={
            "call_id": "call-press",
            "session_id": "call-press",
            "sip_session_id": "sip-press",
            "transport": transport,
        },
    )

    down, up = await run_test(
        capture,
        frames_to_send=[_RunToolFrame(arguments={"digits": "123"})],
    )

    _assert_no_forbidden_frames(down, "downstream")
    _assert_no_forbidden_frames(up, "upstream")

    # Side check: the executor really did run and call send_dtmf with
    # the documented payload shape (the rewrite must not have
    # accidentally bypassed the transport).
    assert capture.tool_result is not None
    transport.send_dtmf.assert_awaited_once()
    settings = transport.send_dtmf.await_args.args[0]
    assert settings["tones"] == "123"
    assert settings["sessionId"] == "sip-press"
    assert settings["digitDurationMs"] == 120


# ── transfer_call ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transfer_call_via_run_test_does_not_emit_pipeline_control_frames():
    """Regression guard for ``transfer_call``. Standard pattern: calls
    ``transport.sip_call_transfer`` directly. No frame pushes.
    """
    transport = MagicMock()
    transport.sip_call_transfer = AsyncMock(return_value=None)

    capture = ToolEmissionCapture(
        transfer_call_executor,
        ctx_kwargs={
            "call_id": "call-transfer",
            "session_id": "call-transfer",
            "sip_session_id": "sip-transfer",
            "transport": transport,
            "tool_settings": {"targets": {"billing": "+15551234567"}},
        },
    )

    down, up = await run_test(
        capture,
        frames_to_send=[_RunToolFrame(arguments={"target": "billing"})],
    )

    _assert_no_forbidden_frames(down, "downstream")
    _assert_no_forbidden_frames(up, "upstream")

    transport.sip_call_transfer.assert_awaited_once()


# ── end_call ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_end_call_via_run_test_emits_end_task_frame_upstream_and_no_interrupts():
    """Regression guard for ``end_call``. The May 2026 rewrite pushes
    ``EndTaskFrame`` UPSTREAM (the documented Pipecat pattern,
    avoiding the ``EndFrame`` hang race in pipecat issue #3757).

    Asserts:

    * ``EndTaskFrame`` appears in the upstream captures.
    * No ``InterruptionTaskFrame`` / ``InterruptionFrame`` /
      ``CancelTaskFrame`` in either direction.
    """
    capture = ToolEmissionCapture(
        end_call_executor,
        ctx_kwargs={
            "call_id": "call-end",
            "session_id": "call-end",
            "sip_session_id": "sip-end",
        },
    )

    down, up = await run_test(
        capture,
        frames_to_send=[_RunToolFrame(arguments={"reason": "done"})],
    )

    _assert_no_forbidden_frames(down, "downstream")
    _assert_no_forbidden_frames(up, "upstream")

    end_task_frames = [f for f in up if isinstance(f, EndTaskFrame)]
    assert end_task_frames, (
        f"no EndTaskFrame in upstream captures — end_call's "
        f"shutdown signal isn't reaching PipelineTask. Got upstream: "
        f"{[type(f).__name__ for f in up]}"
    )
    # Reason field carries the configured prefix so call-record audits
    # can correlate end-call decisions back to the tool path.
    assert "end_call_tool" in str(end_task_frames[0].reason or "")
