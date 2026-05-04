"""Platform-wide engine settings — env-var-backed, pydantic-settings.

This module owns every operator-tunable platform value. Per-agent
config (LLM model, voice ID, prompt, tools…) lives in :mod:`agent_config`
and comes from the API Lambda's runtime-config endpoint; this layer
covers the values that are *engine-wide*, not per-call.

Why env-vars only
-----------------

v1 used SSM Parameter Store for hot-tunable feature flags. Every
feature that justified that machinery (knowledge-base / RAG, A2A
capability registry, SageMaker provider routing) was dropped from
v2's surface. Pipecat's own deployment guidance uses plain env vars,
and Fargate task-definition redeploys are cheap, so the SSM
round-trip at every container boot buys nothing in v2.

Why no singleton
----------------

v1's ``ConfigService.get_config_service()`` was a module-level
singleton. v2 builds ``Settings`` once at process startup (in the
runtime layer) and passes it explicitly to anything that needs it.
Dependency injection rather than global state.

Locked-in technical choices live in code, not here
--------------------------------------------------

These are NOT settings fields, even though v1 sometimes treated them
as configurable:

- STT vendor (AssemblyAI), TTS vendor (ElevenLabs), LLM vendor (Bedrock)
- AssemblyAI sample rate (8000), encoding (pcm_s16le), turn mode (Mode 2)
- VAD thresholds, MinWords value, smart-turn analyzer choice

Changing any of those should require a code review, not an env-var
flip. They live in Layer 3 (services factory) and Layer 8 (pipeline
builder).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Platform-wide engine settings.

    Two fields are required (no default — :class:`Settings` raises
    :class:`pydantic.ValidationError` at construction if they aren't
    set in the environment). Everything else has a sensible default.

    Field names are lowercase Python convention; ``case_sensitive=False``
    makes both ``VOICE_API_LAMBDA_NAME`` (the standard env-var
    convention) and ``voice_api_lambda_name`` resolve to the same
    attribute.
    """

    # ── Required at boot ────────────────────────────────────────────────
    # No defaults. Settings() raises ValidationError if these aren't set.

    voice_api_lambda_name: str
    """The cosentus-voice-api Lambda function name (or alias) the engine
    invokes for ``runtime-config``, ``call-write``, etc. Production
    sets this to e.g. ``medcloud-voice-api:live``."""

    api_key_secret_arn: str
    """Secrets Manager ARN whose JSON blob holds Daily / ElevenLabs /
    AssemblyAI API keys. The secrets-loader reads this at boot and
    populates the matching env vars."""

    # ── Infrastructure with sensible defaults ───────────────────────────

    aws_region: str = "us-east-1"
    """Region for every boto3 client. Matches the Fargate task's region."""

    environment: str = "production"
    """Deployment environment tag for structured logs and CloudWatch EMF."""

    log_level: str = "INFO"
    """Engine log level. ``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR``."""

    service_port: int = 8080
    """Port the FastAPI HTTP entrypoint binds on inside the Fargate task."""

    max_concurrent_calls: int = 6
    """Per-task maximum concurrent voice sessions. Auto-scaling adds
    tasks above this threshold; concurrency above this on a single
    task degrades audio quality. Initial value 6 is a starting point;
    Layer 9.5 scale testing will validate or adjust to 4 or 8."""

    # ── Layer 9 (runner) ────────────────────────────────────────────────

    daily_api_key: str = ""
    """Daily.co API key. Used by Layer 9's ``DailyRoomClient`` to
    create rooms and mint meeting tokens. Empty default permits
    boot in a test environment without Daily access; the runner
    itself fails fast at room-creation time if the key is unset."""

    recording_bucket: str = ""
    """S3 bucket Daily uses for cloud recording uploads. Set on each
    room's ``recordings_bucket.bucket_name`` property at room
    creation time. Empty default means recordings are disabled —
    the room is created without ``recordings_bucket``, falling back
    to Daily's default storage. Production sets this."""

    recording_role_arn: str = ""
    """IAM role ARN Daily assumes when writing recordings to the
    bucket. Required when ``recording_bucket`` is set; ignored
    otherwise. Set on each room's ``recordings_bucket.assume_role_arn``
    property at room creation time. The trust policy on the role
    grants Daily's signing principal sts:AssumeRole; the role's
    permission policy grants s3:PutObject on the bucket."""

    # ── Operator kill-switch ────────────────────────────────────────────

    disabled_tools: str = ""
    """CSV of tool names to disable platform-wide. Empty means none.
    Useful when a single tool is misbehaving and needs to be turned
    off across every agent without redeploying. The tools layer
    parses the CSV — Settings stores it as a raw string."""

    model_config = SettingsConfigDict(
        # ``.env`` is for local dev only. Production Fargate sets env
        # vars via the task definition; .env is not present in the
        # container image.
        env_file=".env",
        env_file_encoding="utf-8",
        # Fargate sets a slew of AWS-managed env vars
        # (ECS_CONTAINER_METADATA_URI_V4, AWS_EXECUTION_ENV, etc.)
        # that aren't ours to model. Drop them silently.
        extra="ignore",
        # Standard env-var convention is uppercase; field names
        # follow Python convention. Both should resolve.
        case_sensitive=False,
    )
