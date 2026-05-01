# Migration from v1

## Why a new repo

`cosentus-voice-engine` (v1) is a fork of the AWS
`sample-voice-agent` reference project. It accumulated four kinds of
debt:

- **Upstream cruft we don't use** — A2A capability agents, SageMaker
  bidirectional STT/TTS, Deepgram and Cartesia provider paths, dual
  `ecs_main.py` / `local_main.py` entrypoints, parallel
  `pipeline_local.py` / `pipeline_ecs.py` pipeline builders.
- **Debug-driven sprawl** — `observability.py` grew to a 2200-line
  monolith holding ten unrelated observers; `pipeline_ecs.py` carries
  a dead `_register_capabilities()` block that imports a deleted
  module. See v1's `docs/audits/state-of-codebase-2026-04-29.md` for
  the full inventory.
- **Documentation drift** — README still describes the upstream A2A
  hub-and-spoke architecture, IAM still grants CloudMap and Bedrock
  Knowledge-Base permissions for features that were removed at the
  Python layer.
- **Contract drift** — see v1's
  `docs/audits/agent-persistence-contract-trace-2026-05-01.md`:
  five frontend endpoints 404 against the API Lambda; per-agent STT
  and TTS provider fields are silently overridden by env vars; the
  version archive table is empty.

A refactor in place would be a 5000-line PR touching every file. A
greenfield rebuild, with the v1 repo as read-only reference and the
v2 repo as a leaves-first translation, is faster, cleaner, and easier
to AI-assist. v1 stays in production until v2 cuts over.

## The migration sequence

We rebuild in twelve dependency-ordered layers, leaves of the
dependency tree first. Each layer is read in v1 → summarized →
proposed → reviewed → implemented with tests → moved on. We do not
propose multiple layers ahead. We do not write code until told.

| Layer | Scope | One-line description |
|-------|-------|----------------------|
| 0 | Repo skeleton | Directory tree, `.gitignore`, `pyproject.toml`, this doc, the architecture overview. |
| 1 | Config models | `AgentConfig` Pydantic models and the `runtime-config` HTTP fetch. |
| 2 | Settings | Environment + SSM bootstrap; one place that knows where everything lives. |
| 3 | Service factory | STT (AssemblyAI), TTS (ElevenLabs), LLM (Bedrock) construction with the nine locked-in choices. |
| 4 | Tools | Built-in tool catalog: `end_call`, `transfer_call`, `press_digit`, `time`. |
| 5 | Prompt | Prompt hydration: agent config + first message → final system prompt. |
| 6 | Persistence | Call lifecycle writer + post-call analysis writer (HTTP to API Lambda). |
| 7 | Observers | Split v1's `observability.py` monolith into focused observer files (metrics, transcripts, turn timing, errors, recording-state). |
| 8 | Pipeline | Single Pipecat pipeline builder. Composes layers 1–7 into the runtime. |
| 9 | Runtime | FastAPI HTTP entrypoint, session lifecycle, Fargate task protection. |
| 10 | Bot-runner Lambda | Daily webhook → Fargate bridge. Phone-number resolver, HMAC verifier, dial-out client. |
| 11 | CDK | Network, storage, ECS, bot-runner stacks; reusable constructs. |

Layers 1–7 are leaves of the dependency tree (each depends only on
earlier layers). Layer 8 is the integration point. Layers 9–11 wrap
the engine for production deployment.

## Cutover plan

1. **Layer-by-layer build in v2.** Each layer ships with passing
   tests before the next layer starts. v1 is unchanged.
2. **Dev deploy.** Once Layer 11 lands, deploy the v2 stack into the
   dev AWS account alongside v1. Same Aurora, same SSM, same KMS
   key. New ECS service, new bot-runner Lambda, new Daily app
   pointed at the new endpoints.
3. **Real call testing.** Run an end-to-end test matrix against the
   dev stack: outbound to a known IVR sandbox, inbound to a test
   number, recording lifecycle, post-call analysis, scaling under
   load. Verify each of the nine locked-in choices is wired
   correctly by inspecting structured logs.
4. **Prod cutover.** Update the prod Daily app's webhook URL and the
   prod phone-number routing to point at the v2 bot-runner. Drain
   v1's ECS service. Watch the next 24 hours of call records.
5. **Archive v1.** Once v2 has run prod for a week with no
   regressions, archive `cosentus-voice-engine` to read-only and
   delete its CDK stacks. The audit docs in v1's `docs/audits/`
   remain as historical reference.
