"""Service-construction layer.

Pure functions that build the three vendor service objects the
pipeline consumes: AssemblyAI STT, ElevenLabs TTS, AWS Bedrock LLM.
No pipeline composition lives here — that's Layer 8.
"""

from app.services.factory import (
    build_llm,
    build_stt,
    build_tts,
    resolve_bedrock_model_id,
)

__all__ = [
    "build_llm",
    "build_stt",
    "build_tts",
    "resolve_bedrock_model_id",
]
