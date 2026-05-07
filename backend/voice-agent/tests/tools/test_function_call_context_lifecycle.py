"""VERIFICATION-2 — function-call lifecycle integration tests.

Empirically confirms (via Pipecat's official ``pipecat.tests.utils.
run_test`` harness) that the standard function-call frame sequence
results in BOTH a ``tool_use`` block (assistant turn) AND a
``tool_result`` block (user turn) landing in the LLM context. The
Bedrock adapter then converts those into ``toolUse`` / ``toolResult``
content blocks Claude can reason over.

This is the specific regression yesterday's inbound PSTN test
surfaced: because press_digit pushed an ``InterruptionTaskFrame``
from inside the handler, neither the tool_use nor the tool_result
ever made it into context. Claude saw repeated user requests but
no record of having pressed digits → infinite tool-call loop. The
press_digit rewrite removed the interruption hack; these tests are
the empirical guard that the lifecycle now works end-to-end.

Why ``run_test`` (and not direct method invocation)? The bug was a
frame-flow problem — the ``FunctionCallInProgressFrame`` and
``FunctionCallResultFrame`` (both ``UninterruptibleFrame``) failed
to reach the assistant aggregator because of an in-flight
``InterruptionFrame`` racing them. A test that directly calls
``_handle_function_call_in_progress`` would pass even if the
underlying frame-flow path were still broken. ``run_test`` exercises
the real Pipeline → Source → Processor → Sink path, so it would
have caught the bug before it shipped.

Reference: https://reference-server.pipecat.ai/en/stable/api/pipecat.tests.utils.html
"""

from __future__ import annotations

import json

import pytest
from pipecat.adapters.services.bedrock_adapter import AWSBedrockLLMAdapter
from pipecat.frames.frames import (
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    FunctionCallResultProperties,
    LLMContextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregator,
)
from pipecat.tests.utils import run_test


def _in_progress(tool_call_id: str = "tc_lifecycle_1") -> FunctionCallInProgressFrame:
    return FunctionCallInProgressFrame(
        function_name="press_digit",
        tool_call_id=tool_call_id,
        arguments={"digits": "123"},
        cancel_on_interruption=True,
    )


def _result(tool_call_id: str = "tc_lifecycle_1") -> FunctionCallResultFrame:
    return FunctionCallResultFrame(
        function_name="press_digit",
        tool_call_id=tool_call_id,
        arguments={"digits": "123"},
        result={"digits_pressed": "123", "digit_count": 3},
        run_llm=True,
        properties=FunctionCallResultProperties(),
    )


@pytest.mark.asyncio
async def test_lifecycle_via_run_test_records_tool_use_and_tool_result_in_context():
    """The canonical FunctionCallInProgress → FunctionCallResult sequence,
    sent through Pipecat's ``run_test`` harness, results in:

    * an ``assistant`` message with a ``tool_calls`` list naming the
      tool, AND
    * a ``tool``-role message with the actual result payload (no
      stale ``IN_PROGRESS`` marker).

    Both must be present so the Bedrock adapter can emit the proper
    ``toolUse`` and ``toolResult`` content blocks Claude needs to
    keep memory of what it called.
    """
    context = LLMContext()
    aggregator = LLMAssistantAggregator(context=context)

    await run_test(
        aggregator,
        frames_to_send=[_in_progress(), _result()],
    )

    messages = context.get_messages()

    asst_with_tool_calls = [
        m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert len(asst_with_tool_calls) == 1, messages
    tc = asst_with_tool_calls[0]["tool_calls"][0]
    assert tc["id"] == "tc_lifecycle_1"
    assert tc["function"]["name"] == "press_digit"

    tool_msgs = [
        m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == "tc_lifecycle_1"
    ]
    assert len(tool_msgs) == 1, messages
    assert tool_msgs[0]["content"] != "IN_PROGRESS", (
        "tool message must be updated with the actual result; got the stale "
        "IN_PROGRESS placeholder, which means _handle_function_call_finished "
        "didn't run — the Bug A symptom returning."
    )
    payload = json.loads(tool_msgs[0]["content"])
    assert payload["digits_pressed"] == "123"


@pytest.mark.asyncio
async def test_lifecycle_via_run_test_pushes_context_frame_upstream_when_run_llm_true():
    """When the FunctionCallResultFrame has ``run_llm=True``, the
    aggregator pushes an ``LLMContextFrame`` upstream so the LLM
    service re-fires with the updated context. ``run_test``'s
    upstream capture proves the frame flows correctly — without it,
    Claude wouldn't get a chance to confirm the tool result.
    """
    context = LLMContext()
    aggregator = LLMAssistantAggregator(context=context)

    _, up_frames = await run_test(
        aggregator,
        frames_to_send=[_in_progress("tc_run_llm"), _result("tc_run_llm")],
    )

    context_frames = [f for f in up_frames if isinstance(f, LLMContextFrame)]
    assert context_frames, (
        f"no LLMContextFrame upstream — re-inference path is broken. "
        f"Got upstream frames: {[type(f).__name__ for f in up_frames]}"
    )


@pytest.mark.asyncio
async def test_bedrock_adapter_emits_tool_use_and_tool_result_blocks_via_run_test():
    """End-to-end: drive the full lifecycle via ``run_test``, then
    convert the resulting LLMContext through ``AWSBedrockLLMAdapter``
    and assert the Bedrock-format messages contain:

    * a ``toolUse`` content block on an ``assistant`` turn
    * a ``toolResult`` content block on a ``user`` turn

    This is the production-shape proof — what Claude actually sees
    on its next inference call.
    """
    context = LLMContext()
    aggregator = LLMAssistantAggregator(context=context)
    context.add_message({"role": "user", "content": "Press 1 2 3 please."})

    await run_test(
        aggregator,
        frames_to_send=[_in_progress("tc_bedrock"), _result("tc_bedrock")],
    )

    adapter = AWSBedrockLLMAdapter()
    invocation = adapter.get_llm_invocation_params(context)
    bedrock_messages = invocation["messages"]

    tool_use_blocks: list = []
    tool_result_blocks: list = []
    for msg in bedrock_messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if "toolUse" in block:
                tool_use_blocks.append((msg["role"], block["toolUse"]))
            if "toolResult" in block:
                tool_result_blocks.append((msg["role"], block["toolResult"]))

    assert tool_use_blocks, (
        f"no toolUse content block in Bedrock messages; this is the "
        f"Bug A regression. Bedrock messages: {bedrock_messages}"
    )
    assert tool_result_blocks, (
        f"no toolResult content block in Bedrock messages; this is the "
        f"Bug A regression. Bedrock messages: {bedrock_messages}"
    )

    role, tu = tool_use_blocks[0]
    assert role == "assistant", f"toolUse landed on wrong role: {role}"
    assert tu["toolUseId"] == "tc_bedrock"
    assert tu["name"] == "press_digit"
    assert tu["input"] == {"digits": "123"}

    role, tr = tool_result_blocks[0]
    assert role == "user", f"toolResult landed on wrong role: {role}"
    assert tr["toolUseId"] == "tc_bedrock"
    assert tr["content"], f"toolResult content empty: {tr}"
