# Architecture overview

This document is the system spec for `cosentus-voice-engine`. It is the
source of truth that the eleven implementation layers below Layer 0
build toward. If something here is wrong, fix it here first; do not
let code drift from this doc.

## What this is

The Cosentus voice agent platform powers AI voice agents that handle
phone calls for medical billing (RCM). The two production call shapes
are:

- **Outbound** — the engine dials an insurance company on behalf of a
  Cosentus biller, navigates the IVR, asks about claim status,
  records the answer, and writes it back. Calls are scheduled in
  batches against a payer; concurrency and calling-hour windows are
  per-agent.
- **Inbound** — a patient or provider calls a Cosentus number, the
  engine picks up, handles billing-support questions, and either
  resolves the call or transfers to a human.

Volume targets: thousands of calls per week, multiple concurrent at a
time, auto-scaling on Fargate based on session count. HIPAA-adjacent
data flows through every call — recordings are encrypted at rest with
SSE-KMS and access-controlled at the Lambda layer.

## The four layers of the platform

The platform is a four-layer system. This repo owns one of them.

| Layer | Owner | Responsibility |
|-------|-------|----------------|
| **Data** | sibling repo / shared infra | Aurora Postgres: `voice_agents`, `voice_calls`, batch state, phone numbers. SSM holds platform-wide secrets. S3 holds recordings, encrypted with KMS. |
| **API** | `cosentus-voice-api-lambda` (separate repo) | Node Lambda behind API Gateway. CRUD for agents, batches, calls. Owns the schema and all writes to Aurora. The engine consumes `GET /api/agents/:id/runtime-config` and posts back call lifecycle events. |
| **Engine** | **this repo** | The Pipecat pipeline that runs each call. The HTTP entrypoint Fargate exposes. The bot-runner Lambda that bridges Daily webhooks into Fargate. The CDK that deploys all of it. |
| **Telephony** | Daily.co (third party) | PSTN trunks, SIP, the WebRTC transport that Pipecat speaks, and cloud recording into our S3 bucket. |

## End-to-end call flow

### Outbound batch

A biller schedules a batch of outbound calls in the frontend. The API
Lambda inserts a `voice_calls` row per target with `status='queued'`,
respecting the agent's `default_concurrency` and calling-window. A
dispatcher Lambda walks the queue, fetches the agent's `runtime-config`
from the API, and POSTs Daily to start a dial-out. Daily creates a
session, dials the destination, and fires its `meeting.started`
webhook at the bot-runner Lambda. The bot-runner resolves which agent
should handle the call from the Daily room metadata, then invokes the
Fargate engine's HTTP `/start` endpoint with the agent id and the
Daily room URL + token. The engine connects Pipecat to the Daily
room; from that point STT → LLM → TTS audio flows over the active
WebRTC session until either side hangs up. On hangup the engine
writes a final call record and triggers post-call analysis.

### Inbound

A caller dials a Cosentus number. Daily picks up via PSTN, looks up
the inbound agent for that number, creates a session, and fires
`meeting.started` at the bot-runner. From there the path is identical
to outbound: bot-runner resolves the agent, calls the Fargate `/start`
endpoint, the engine attaches Pipecat to the Daily room, conversation
runs, hangup writes the call record.

## Deliberate Cosentus choices

These nine choices are the lessons paid for in v1's production
debugging. They are non-negotiable; v2 preserves them and inline
comments in the v2 source explain *why* each one differs from a stock
Pipecat template. Numbers map 1:1 to the project brief.

1. **AssemblyAI Mode 2 turn detection** — `vad_force_turn_endpoint=False`.
   AssemblyAI's native turn endpoint is more accurate than Silero VAD
   on phone audio because it sees the same signal the model trained
   on. We let the STT decide when a turn ends; we do not force the
   issue from a separate VAD analyzer.

2. **`MinWordsUserTurnStartStrategy(min_words=3)` as the sole start
   strategy** — no `VADUserTurnStartStrategy`. On phone calls the
   first second of every turn is full of breath, room noise, and
   carrier artifacts that look like speech to a VAD. Requiring three
   words before we declare a turn started prevents "ghost turns" that
   would otherwise kick the LLM into a half-completed prompt.

3. **`should_interrupt=False` on the AssemblyAI service** — plugs a
   framework bypass where AssemblyAI's interim transcripts would
   reach the interrupter ahead of the turn manager and interrupt the
   bot mid-utterance on phantom user speech.

4. **`vad_threshold=0.3` on AssemblyAI, matched by Silero confidence
   `0.3`** — phone-audio energy is lower than studio audio. The stock
   `0.5` thresholds drop legitimate quiet speech (older callers,
   speakerphone, hold-music transitions). 0.3 was tuned against real
   call recordings.

5. **AssemblyAI at `sample_rate=8000`, `encoding=pcm_s16le`** — Daily
   PSTN delivers 8 kHz PCM-linear, not mulaw. We send the STT exactly
   what the wire delivers; no upsampling, no transcode.

6. **ElevenLabs as TTS, with per-agent voice ID and TTS settings from
   Aurora** — voice identity is part of the agent's product
   personality. `tts_voice_id`, `stability`, `similarity_boost`,
   `style`, `speed`, `use_speaker_boost` are agent-config fields that
   round-trip from the editor through Aurora into the pipeline.

7. **AWS Bedrock for LLM, with per-agent model selection from Aurora**
   — Cosentus standardises on Claude (currently the Sonnet-4-6 /
   Haiku-4-5 family). Per-agent model lets us route cheap calls to
   Haiku and reserve Sonnet for harder tasks without code changes.

8. **Daily.co cloud recording into S3 with SSE-KMS** — recording is
   first-class. Every call writes a recording artifact; access is
   gated by the API Lambda which mints presigned URLs against the
   KMS key. No path bypasses the KMS-encrypted bucket.

9. **`LocalSmartTurnAnalyzerV3` as the stop-detection turn analyzer**
   — wired into `UserTurnStrategies.stop` via
   `TurnAnalyzerUserTurnStopStrategy`. Pipecat's default
   `stop_secs=3.0` produces 3 full seconds of dead air after VAD
   silence. Cosentus tunes `stop_secs` lower so the bot responds
   conversationally; the smart-turn model still gates the decision so
   we don't talk over the caller.

## What this system explicitly does NOT have

The v2 repo deletes the following, which existed in v1 (because v1
forked the AWS `sample-voice-agent`):

- **No A2A capability agents.** No hub-and-spoke handoff to
  capability agents over CloudMap / service-discovery. The single
  Pipecat pipeline does the whole call. Multi-agent handoff, if it
  ships, will be Pipecat Flows-based and is out of scope for v2.
- **No SageMaker self-hosted STT/TTS.** AssemblyAI + ElevenLabs only,
  via their cloud APIs. The custom `DeepgramSageMakerTTSService` and
  `DeepgramSageMakerSTTService` are not migrated.
- **No local-only pipeline path.** v1 had `pipeline_local.py` and
  `pipeline_ecs.py` as parallel pipeline builders for local
  prototyping vs production. v2 has one pipeline. If a developer
  needs to test locally, they run the same pipeline against a Daily
  dev room — not a forked code path.
- **No hardcoded fallback agent.** v1 had a "default agent" baked in
  to keep the pipeline alive when Aurora was unreachable. v2 fails
  the call cleanly and lets retry handle it.
- **No Deepgram, no Cartesia.** Removed from the dependency surface.
- **No bidirectional SageMaker streaming client.** Removed.
- **No `ecs_main.py` / `local_main.py` dual entrypoints.** One
  HTTP entrypoint that Fargate runs. Period.

## Scaling story

Fargate runs the engine container as a service behind an internal
ALB. Each task hosts a single Pipecat process; concurrency comes from
horizontal scaling, not threading. Capacity is sized by **session
count**: a session-counter Lambda polls Daily's REST API on a 30s
cadence, exports a CloudWatch metric (`SessionsPerTask`), and
auto-scaling adds tasks above a target threshold and drains below
it. Task-protection ensures a scaling-in task is not killed while a
call is active; the engine sets task protection on `/start` and
clears it after hangup.

Per-call resources (Daily room, STT/TTS connections, Bedrock client)
are isolated to the task. Aurora is reached via the API Lambda over
HTTPS — the engine never opens a Postgres connection itself, which
keeps Aurora connection counts bounded by Lambda concurrency rather
than by Fargate fleet size. Recordings stream from Daily directly
into S3; the engine never proxies media bytes.

The platform's failure modes are independent: a Daily outage stops
new calls but lets in-flight calls finish; an Aurora outage stops new
calls (no `runtime-config`) but lets in-flight calls finish; a
Fargate task crash drops only the calls on that task and Daily
recordings still land in S3 because cloud recording is server-side.
