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

## Entry 3: Layer 1 falls back to `os.environ` when no Settings is passed

**What.** `load_agent_config` and the module-level `_LAMBDA_CLIENT`
both read `os.environ` directly when no `Settings` object is
provided. As of Layer 2, `Settings` exists, but Layers 3–8 are
written before Layer 9 (runtime). Until Layer 9 lands, callers
either pass `Settings` explicitly or fall back to env reads.

**Why we shipped it.** Layer 9 (runtime) is the layer that will
construct `Settings` once at process startup and pass it through
every Layer-1 call site. Removing the fallback now would require
each Layer 3–8 consumer to plumb a Settings instance through
itself before Layer 9 has a place to construct one. Keeping the
fallback lets each layer ship cleanly and Layer 9 wire everything
up at the end.

**Cost.** Two `os.environ.get` paths in `load_agent_config` (the
`settings is None` branch) plus one in the module-level
`_LAMBDA_CLIENT` constructor (covered in entry 4 too — the AWS
region specifically). Mild duplication of the "where do values
come from" question across two layers.

**Exit condition.** When Layer 9 (`app/runtime/`) lands and:

1. Constructs `Settings()` once at process startup; and
2. Passes `settings` to every `load_agent_config` call site.

At that point we drop the `settings is None` branch from
`load_agent_config` and require a `Settings` argument. The
module-level `_LAMBDA_CLIENT`'s region migration is tracked
separately in entry 4.

**Layer / file.** Layer 1 — `backend/voice-agent/app/config/agent_config.py`.

## Entry 4: `_LAMBDA_CLIENT` region captured at import time

**What.** `agent_config.py` constructs the boto3 lambda client at
module import using `os.environ.get("AWS_REGION", "us-east-1")`.
This happens before any `Settings` object exists.

**Why we shipped it.** A module-level client is the AWS-documented
multithreading pattern (one shared thread-safe client across
worker threads), and that requires the region at construction. The
import order in v2 — Layer 1 modules load before any Layer 9
runtime can run — means there is no `Settings` instance available
at import time without inverting the boot sequence.

**Cost.** Region cannot be changed without a process restart.
Theoretical debt: AWS region is set once at Fargate deploy time
and never changes during the life of a task, so this is more a
shape/cleanliness concern than an operational one.

**Revisit when.** Layer 9 wires up. Two options at that point:

1. Lazily construct the lambda client on first use using
   `settings.aws_region`. The client is then still module-shared
   (created once, kept across calls), just deferred. Trade-off:
   first call pays the construction cost (~1ms).
2. Accept that AWS region is a deploy-time concern and document
   the import-time read as intentional. No code change.

**Layer / file.** Layer 1 —
`backend/voice-agent/app/config/agent_config.py` module-level
`_LAMBDA_CLIENT`.

## Entry 5: `disabled_tools` stored as raw CSV string

**What.** `Settings.disabled_tools` is typed as `str`, not
`list[str]`. Pydantic Settings supports JSON-encoded list strings
from env vars (e.g. `'["a","b"]'` → `["a", "b"]`) but not bare
CSVs without a custom validator. The operator-friendly format is
`"tool1,tool2"`, and `Settings` stores it verbatim.

**Why we shipped it.** A custom field validator is premature
complexity for a field whose only consumer is Layer 4 (tools).
Splitting the string at the consumption site is a one-liner.

**Cost.** `split(",")` lives in Layer 4 rather than centralized
in `Settings`. Mild concern that nothing forces operators to use
the exact format the consumer expects (whitespace, empty entries,
unknown tool names all pass `Settings` validation untouched).

**Revisit when.** Layer 4 ships. If the parsing turns out to need
non-trivial work (whitespace stripping, empty-entry filtering,
validation against the registered tool catalog), promote to a
Pydantic field validator on `Settings.disabled_tools` so the
parsed `list[str]` is the public API and the wire format stays
human-friendly CSV.

**Layer / file.** Layer 2 —
`backend/voice-agent/app/config/settings.py` (`disabled_tools`
field).
