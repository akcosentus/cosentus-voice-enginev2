# v2 tech debt log

Open issues we know about and have decided to defer. Each entry has
context (what), rationale (why we accepted it), and an exit
condition (when we close it). Order is chronological; numbers are
stable references for code comments.

## Entry 1: Lambda `runtime-config` carries fields v2 doesn't model

**Context.** The cosentus-voice-api Lambda's
`GET /api/agents/:id/runtime-config` endpoint returns several fields
that v2's `AgentConfig` does not model:

- `llm.provider`, `llm.enable_prompt_caching`
- `tts.provider`, `tts.settings.similarity_boost`,
  `tts.settings.style`, `tts.settings.speed`
- `stt.provider`, `stt.language`
- `recording.enabled`, `recording.channels` (the entire
  `recording` object)

v2 silently drops them via `extra='ignore'` on the Pydantic models.

**Why we accepted this.** v2 deliberately collapses several v1
choices that were per-agent on paper but platform-wide in practice.
v1's contract-trace audit (handoff #6) confirmed the engine ignored
these fields at runtime anyway — env vars and Daily defaults won.
v2 names that truth in the type system: per-agent provider/recording
fields are not modeled because they are not honored. Changing the
lambda's response contract to drop the fields is a separate repo's
work and out of scope for v2's greenfield rebuild.

**Cost.** Minor. The lambda continues to ship bytes we throw away;
a future contract change in either direction (lambda removes the
fields, or v2 wires them through) will be a coordinated edit.

**Exit condition.** Close this entry when the cosentus-voice-api
Lambda repo stops sending the dropped fields (or marks them
deprecated and the next contract revision removes them). At that
point we drop `extra='ignore'` from the AgentConfig submodels so
contract drift produces loud failures instead of silent ones.

**Layer / file.** Layer 1 — `backend/voice-agent/app/config/agent_config.py`.

## Entry 2: `AgentConfigMeta.version` → `updated_at_ms` alias

**Context.** The lambda's `_meta.version` field is Aurora's
`updated_at` column rendered as unix milliseconds — not a real
version number. v2's `AgentConfigMeta` exposes the value as
`updated_at_ms` for clarity, with a Pydantic field alias on the
wire name `version` (`Field(default=0, alias="version")`). The
alias is the only reason the data round-trips; without it,
`extra='ignore'` would drop the value silently and every call
would log `updated_at_ms=0`.

**Why we accepted this.** Aligning the lambda contract is part of
the lambda repo's P5 cleanup work (see v1's contract-trace audit).
Decoupled from v2's greenfield rebuild — v2 papers over the
upstream misnaming so internal callers see the honest name today.

**Cost.** Minor. A workaround that papers over upstream misnaming.
Anyone reading `AgentConfigMeta` for the first time has to follow
the alias to understand the wire format.

**Exit condition.** Close when the lambda repo's P5 cleanup
renames the field on the wire. At that point we drop the
`Field(alias="version")` so `updated_at_ms` is just the wire
field name.

**Layer / file.** Layer 1 — `backend/voice-agent/app/config/agent_config.py`
(`AgentConfigMeta`).

## Entry 3: Layer 1 reads env vars directly; Layer 2 will own settings

**Context.** `app/config/agent_config.py` reads
`VOICE_API_LAMBDA_NAME` and `AWS_REGION` directly from `os.environ`.
v2 plans a dedicated settings layer (Layer 2) that owns all
env-and-SSM bootstrap; the loader should consume from it rather
than reach into `os.environ` itself.

**Why we accepted this.** Layer 2 doesn't exist yet. Adding inline
`os.environ.get(...)` calls is a two-line workaround; abstracting
prematurely would violate the "no premature abstraction" principle.

**Cost.** Minimal. Two `os.environ.get` calls become a `Settings`
field read when Layer 2 lands.

**Exit condition.** When Layer 2 (`app/config/settings.py`) lands,
refactor `load_agent_config` to take a `Settings` instance (or
read from a module-level resolved settings object) instead of
calling `os.environ.get` itself. The module-level
`_LAMBDA_CLIENT`'s region also moves to settings.

**Layer / file.** Layer 1 — `backend/voice-agent/app/config/agent_config.py`.
