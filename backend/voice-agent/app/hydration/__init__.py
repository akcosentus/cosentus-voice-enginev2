"""Prompt hydration — variable substitution for system_prompt + first_message.

Layer 5. The single public function is :func:`hydrate_prompt`. Layer 8
(pipeline builder) calls it twice per call: once for the agent's
``system_prompt`` (passed to the LLM service via ``system_instruction``)
and once for ``first_message`` (spoken via ``TTSSpeakFrame`` if the
agent has a static opener).
"""

from app.hydration.hydrator import hydrate_prompt

__all__ = ["hydrate_prompt"]
