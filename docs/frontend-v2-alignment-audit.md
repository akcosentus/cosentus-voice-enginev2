# Frontend → v2 Alignment Audit

**Author**: Wave-6-followup audit (2026-05-19, read-only)
**Repos inventoried**:

- Frontend: `~/Desktop/voiceagentfront` (Next.js 16 App Router, React 19, native `fetch`)
- v2 engine: `~/Desktop/cosentus-voice-enginev2` (Fargate-hosted Python aiohttp voice engine)
- Shared control plane: `~/Desktop/cosentus-voice-api-lambda` (Node.js 22 Lambda + Aurora)

**This is investigation only — no code changes, no commits.**

---

## 0. TL;DR

The "v1 is dead, frontend talks to v2" mental model is wrong. The actual topology is:

```
                                          ┌──────────────────────────────────────┐
                                          │   cosentus-voice-api-lambda          │
       voiceagentfront ── X-API-Key ────► │   (single Node.js Lambda, Aurora     │
       (CRUD + test-call UI)              │    PostgreSQL, shared between v1+v2) │
                                          └──────────────────────────────────────┘
                                                            ▲
                                                            │ lambda:Invoke
                                                            │ (runtime-config,
                                                            │  phone-lookup,
                                                            │  call-write,
                                                            │  auto-actions)
                                                            │
       voiceagentfront ── X-API-Key + ────────────► ┌──────────────────────────┐
       (test-call panel)   Pipecat WebRTC           │  v2 engine (Fargate)     │
                                                    │  POST /start,            │
                                                    │  POST /daily-dialin-     │
                                                    │  webhook,                │
                                                    │  GET /status, /health,   │
                                                    │  /ready                  │
                                                    └──────────────────────────┘
```

- **44 / 45 frontend endpoints** are CRUD against the shared Lambda. The Lambda is **alive and actively used by v2**.
- **1 / 45 frontend endpoint** (`POST /api/test-call/connect` via Pipecat WebRTC) targets the v2 engine. **This is the only true frontend↔v2 integration point, and it's the biggest mismatch.**
- v1 engine fork is dead. The shared Lambda is not v1 — it's the **shared control plane**.
- Almost all production deploy work is **path/contract drift between the frontend and the shared Lambda**, not v2-specific. Most of these can be fixed with single-line route changes on the Lambda OR one-line `api.ts` edits on the frontend.
- The only large piece of work is **wiring the test-call panel to v2's `POST /start { direction: "browser" }` + Daily Web SDK** instead of Pipecat's SmallWebRTC transport.

**Recommended phasing** (see §7):

| Phase | Work | Effort | Risk |
|---|---|---|---|
| P1 | Fix path/method drift between frontend and shared Lambda | Small (~1 day) | Low |
| P2 | Migrate test-call panel to Daily Web SDK against v2 `/start` | Medium (~2-3 days) | Medium |
| P3 | Optional polish: BFF proxy for X-API-Key, recording presign, multipart upload parser | Medium (~2-3 days) | Low |
| **Total** | | **~5-7 days** | |

---

## 1. Architecture context

### What's where

| Component | Repo | Hostname | Purpose |
|---|---|---|---|
| Frontend dashboard | voiceagentfront | (whatever Vercel URL) | CRUD UI for agents, batches, calls, voices, phones; test-call panel |
| Shared API Lambda | cosentus-voice-api-lambda | API Gateway path `/prod/voice/api/*` | All CRUD persistence; agent runtime-config; phone-number lookup; call record writes; auto-actions |
| v2 engine | cosentus-voice-enginev2 | `api.cosentusaibackend.com` (prod), `staging.cosentusaibackend.com` (staging) | Real-time voice pipeline; `/start` to spawn calls; `/daily-dialin-webhook` for inbound PSTN; status / health |
| Aurora PostgreSQL | (shared infra) | private VPC endpoint | `voice_*` tables (agents, calls, batches, voices, phones, drafts, versions, etc.) |
| Daily.co | external | api.daily.co | PSTN bridge + WebRTC rooms |
| ElevenLabs | external | api.elevenlabs.io | TTS (called from v2 engine, not Lambda — Lambda has only stubs) |
| AssemblyAI | external | streaming.assemblyai.com/v3 | STT (called from v2 engine) |
| Bedrock | AWS | regional | LLM (called from v2 engine) |

### What v2 does NOT serve

- Agent CRUD
- Batch CRUD
- Call history
- Voice library
- Phone-number CRUD
- Agent schema (form metadata)

All of the above live exclusively in the shared Lambda. v2's HTTP surface is intentionally tiny: just 5 routes for orchestration.

### What the shared Lambda does NOT serve

- Real-time voice pipeline (v2 engine owns that)
- WebRTC test-call signaling (v2 owns that via `/start { direction: "browser" }`)
- ElevenLabs sync (stubbed)
- Twilio sync / search / buy (stubbed; only Daily provider works)
- S3 presigned URLs (TODO — currently returns raw paths)

---

## 2. Frontend inventory (voiceagentfront)

**Stack**: Next.js 16 App Router, React 19, native `fetch`. **No** axios/SWR/React Query. **No** Next.js `app/api/*` proxy routes. **No** WebSocket/SSE. WebRTC only for test calls.

**Centralization**: nearly all REST traffic goes through one file: `src/lib/api.ts` (~548 lines, 44 named exports). One exception: `src/components/test-call-panel.tsx` duplicates `API_BASE` and uses Pipecat's `startBotAndConnect` instead of `fetch`.

**Base URL**: `process.env.NEXT_PUBLIC_API_BASE_URL` (build-time, browser-visible). Default `http://localhost:8000`. No Next.js rewrites — CORS must allow the dashboard's origin.

**Auth**: `X-API-Key: ${NEXT_PUBLIC_COSENTUS_API_KEY}` on every `api.ts` call. Test call panel does NOT send `X-API-Key`.

**Error handling**: mostly `sonner` toasts; several pages swallow errors silently (calls list, batches list, call sheet refetch, draft batch delete).

### A. Agent management

| Frontend endpoint | Used by | Notes |
|---|---|---|
| `GET /api/agents` | agents list, phone-numbers page, batch new, voice picker | Frontend tolerates `{ agents: [...] }` or bare array |
| `GET /api/agents/:name` | agents detail page, draft init/discard | Unwraps `{ agent }` or `{ data }` wrappers |
| `POST /api/agents` | New Agent button | Body `{ name, display_name }` |
| `PUT /api/agents/:name` | Publish (live config, minus `system_prompt`) | Body is full flat agent payload |
| `DELETE /api/agents/:name` | row menu | |
| `POST /api/agents/:name/clone` | row menu | Body `{ name, display_name }` |
| `GET /api/agent-schema` | create/clone/edit | Returns enums + field ranges + defaults — drives form |
| `GET /api/agents/:name/prompt` | publish + batch new | Response `{ content, prompt_variables }` |
| `PUT /api/agents/:name/prompt` | publish | Body `{ content }` |
| `GET /api/agents/:name/draft` | editor load | 404 = init-from-live UI |
| `PUT /api/agents/:name/draft` | every field patch | Body = partial draft |
| `GET /api/agents/:name/versions` | version history panel | Response `{ versions: [...] }` |
| `POST /api/agents/:name/versions` | publish | Body includes `config_snapshot`, `phone_assignments` |
| `POST /api/test-call/connect` | test-call panel + modal | **Pipecat WebRTC** — body `{ agent_name, use_draft: true, case_data? }` |

### B. Phone numbers

| Frontend endpoint | Used by | Notes |
|---|---|---|
| `GET /api/phone-numbers` | phone list + dropdowns | Response includes `provider`, `daily_number_id`, joined agent objects |
| `POST /api/phone-numbers` | **(no UI today)** | Exported but unused |
| `PUT /api/phone-numbers/:id` | save name + agent assignment + publish | Body `{ friendly_name?, inbound_agent_id?, outbound_agent_id? }` |
| `DELETE /api/phone-numbers/:id` | release confirm | |
| `POST /api/phone-numbers/sync-twilio` | Sync from Twilio button | |
| `GET /api/phone-numbers/search?provider=&areaCode=&...` | Buy modal | |
| `POST /api/phone-numbers/buy` | purchase confirm | Body `{ provider, number, friendly_name }` |

### C. Batch calls

| Frontend endpoint | Used by | Notes |
|---|---|---|
| `GET /api/batches` | batches list | |
| `GET /api/batches/:id` | batch detail | Response `{ batch, calls: [...] }` |
| `GET /api/batches/:id/download-url` | Download Original button | `{ url }` → `window.open` |
| `DELETE /api/batches/:id/draft` | new-batch unmount cleanup | Errors swallowed |
| `POST /api/batches/upload` | file dropzone | **`multipart/form-data`** with `file`, `agent_name`, `from_number` |
| `PUT /api/batches/:id/rows` | Start when edits/exclusions | Body `{ mapping, rows: [{index, phone, data, excluded}] }` |
| `POST /api/batches/:id/start` | Start | Body includes `schedule_mode`, `concurrency`, `timezone`, calling-window fields |
| `POST /api/batches/:id/pause` | Pause button | |
| `POST /api/batches/:id/resume` | Resume button | |
| `POST /api/batches/:id/cancel` | Cancel button | |
| `GET /api/batches/:id/status` | **(only used by orphan modal)** | Polling 5s |
| `GET /api/batches/:id/results` | Download results | **Expects binary blob** — not `{ url }` |

### D. Call history

| Frontend endpoint | Used by | Notes |
|---|---|---|
| `GET /api/calls?page=&page_size=&status=&direction=&agent_display_name=&sort_by=&sort_order=` | calls list | Filter uses `agent_display_name`, not `agent_name` |
| `GET /api/calls/agents` | filter dropdown | |
| `GET /api/calls/:id` | call detail page + sheet | Full record with transcript/recording/analyses |
| `DELETE /api/calls/:id` | row delete | |
| `GET /api/calls/:id/recording-url` | audio player | `{ url }` → `<audio src>` |

### E. Voice library

| Frontend endpoint | Used by | Notes |
|---|---|---|
| `GET /api/voices` | voices page, voice picker, agents page | |
| `POST /api/voices/sync` | **(no UI today)** | |
| `POST /api/voices/lookup` | add-voice modal | Body `{ voice_id }` |
| `POST /api/voices/add` | add-voice modal | Body `{ voice_id, custom_name? }` |
| `POST /api/voices/:id/refresh` | row menu | |
| `DELETE /api/voices/:id` | remove dialog | |
| `GET /api/voices/:id/agents` | pre-delete usage check | |

### F. Configuration / settings

No dedicated settings page. Only `GET /api/agent-schema` qualifies. Dashboard is "Coming Soon" static — no API calls.

### G. Misc

No `/health` calls. App root redirects to `/batches`. Voice `preview_url` plays directly from ElevenLabs CDN URLs returned in `Voice` records — not a Cosentus call.

---

## 3. v2 engine inventory (cosentus-voice-enginev2)

**Five routes total**, all on the single aiohttp app in `backend/voice-agent/app/runner/server.py`. Any other path = 404.

| Method + path | Auth | Purpose |
|---|---|---|
| `GET /health` | None | Liveness (always 200 while process running) |
| `GET /ready` | None | Readiness — 200 healthy, 503 draining or at_capacity. ALB target group health check. |
| `GET /status` | `X-API-Key` | Operational snapshot — `{ active_sessions, max_concurrent, draining, protected, ... }` |
| `POST /start` | `X-API-Key` | Spawn an outbound PSTN call or a browser WebRTC test call (via `direction: "browser"`) |
| `POST /daily-dialin-webhook` | None (TODO signature verification) | Inbound PSTN webhook from Daily SIP gateway |

### Critical: `POST /start`

```jsonc
// outbound (default)
{
  "agent_id": "<slug-or-uuid>",
  "direction": "outbound",           // optional, default
  "target_number": "+1...",
  "from_number": "+1...",
  "case_data": { ... },              // optional
  "batch_id": "...",                 // optional
  "batch_row_index": 0               // optional
}

// browser (test call from dashboard)
{
  "agent_id": "<slug-or-uuid>",
  "direction": "browser",
  "case_data": { ... }               // optional
}
```

**Outbound response (202):**
```json
{ "call_id": "<uuid>", "room_name": "...", "room_url": "https://...daily.co/...", "status": "started" }
```

**Browser response (202):**
```json
{
  "call_id": "<uuid>",
  "room_name": "...",
  "room_url": "https://...daily.co/...",
  "viewer_token": "<daily-meeting-token>",
  "status": "started"
}
```

**Errors**: 401 `{error:"unauthorized"}`, 400 `{error:"invalid_json"|"agent_id_required"|"target_number_and_from_number_required"|"unknown_direction:..."}`, 503 `{status:"rejected",reason:"draining"|"at_capacity"}`.

### Hostnames

- Staging: `https://staging.cosentusaibackend.com`
- Prod: `https://api.cosentusaibackend.com`

### No WebRTC connect endpoint

v2 does NOT host a Pipecat WebRTC signaling endpoint. Browser test calls work like this:

1. Frontend POSTs `/start { direction: "browser" }` to v2 with `X-API-Key`.
2. v2 returns `{ room_url, viewer_token, ... }`.
3. Frontend joins the Daily room client-side using the **Daily Web SDK** (`@daily-co/daily-js`), NOT Pipecat's `SmallWebRTCTransport`.

This is the single biggest contract mismatch in the audit (see §5 Bucket C).

---

## 4. Shared Lambda inventory (cosentus-voice-api-lambda)

Single Node.js 22 Lambda (`medcloud-voice-api` prod, `medcloud-voice-api-dev` dev), API Gateway HTTP proxy, hand-rolled router in `index.mjs`.

**Auth**: **none in code**. The frontend sends `X-API-Key` but the Lambda never reads it. Cognito env vars present but unused. If API Gateway doesn't enforce keys, the REST surface is open.

**Storage**: Aurora `voice_*` tables (agents, drafts, versions, calls, batches, batch_rows, phone_numbers, voices, payer_knowledge, call_templates, auto_actions, costs, scores, inbound_routes, call_requests, system_config) + cross-schema MedCloud RCM reads.

**External**: Daily.co for phone search/buy/release (works); Twilio stubs; ElevenLabs stubs; SQS for batch dial queue; Lambda invoke for bot-runner (v1 path).

### Endpoints relevant to frontend

| Frontend call | Lambda route | Status |
|---|---|---|
| `GET /api/agents` | `GET /api/agents` | ✅ Aligned |
| `GET /api/agents/:name` | `GET /api/agents/:name` | ✅ Aligned |
| `POST /api/agents` | `POST /api/agents` | ✅ Aligned |
| `PUT /api/agents/:name` | `PUT /api/agents/:name` | ✅ Aligned |
| `DELETE /api/agents/:name` | `DELETE /api/agents/:name` | ✅ Aligned |
| `POST /api/agents/:name/clone` | `POST /api/agents/:name/clone` | ✅ Aligned |
| `GET /api/agent-schema` | `GET /api/agent-schema` | ✅ Aligned |
| `GET /api/agents/:name/prompt` | `GET /api/agents/:name/prompt` | ✅ Aligned |
| `PUT /api/agents/:name/prompt` | `PUT /api/agents/:name/prompt` | ✅ Aligned |
| `GET /api/agents/:name/draft` | **`GET /api/agent-drafts/:agent_id`** | ⚠️ Path drift |
| `PUT /api/agents/:name/draft` | **`PUT /api/agent-drafts/:agent_id`** | ⚠️ Path drift |
| `GET /api/agents/:name/versions` | **`GET /api/agent-versions/:agent_id`** | ⚠️ Path drift |
| `POST /api/agents/:name/versions` | **`POST /api/agent-versions`** | ⚠️ Path drift |
| `GET /api/phone-numbers` | `GET /api/phone-numbers` | ✅ Aligned |
| `PUT /api/phone-numbers/:id` | `PUT /api/phone-numbers/:id` | ✅ Aligned |
| `DELETE /api/phone-numbers/:id` | `DELETE /api/phone-numbers/:id` | ✅ Aligned |
| `POST /api/phone-numbers/sync-twilio` | stub (always returns `total:0`) | ⚠️ Stub |
| `GET /api/phone-numbers/search?...` | `GET /api/phone-numbers/search?...` | ✅ Aligned (Daily only) |
| `POST /api/phone-numbers/buy` | `POST /api/phone-numbers/buy` | ✅ Aligned |
| `GET /api/batches` | `GET /api/batches` | ✅ Aligned |
| `GET /api/batches/:id` | `GET /api/batches/:id` | ✅ Aligned |
| `GET /api/batches/:id/download-url` | `GET /api/batches/:id/download-url` | ✅ Aligned (returns raw path, not presigned) |
| `DELETE /api/batches/:id/draft` | `DELETE /api/batches/:id/draft` | ✅ Aligned |
| `POST /api/batches/upload` | **JSON body expected, not multipart** | ❌ Format drift |
| `PUT /api/batches/:id/rows` | `PUT /api/batches/:id/rows` | ✅ Aligned |
| `POST /api/batches/:id/start` | `POST /api/batches/:id/start` | ✅ Aligned |
| `POST /api/batches/:id/pause` | `POST /api/batches/:id/pause` | ✅ Aligned |
| `POST /api/batches/:id/resume` | `POST /api/batches/:id/resume` | ✅ Aligned |
| `POST /api/batches/:id/cancel` | `POST /api/batches/:id/cancel` | ✅ Aligned |
| `GET /api/batches/:id/status` | `GET /api/batches/:id/status` | ✅ Aligned |
| `GET /api/batches/:id/results` | **`GET /api/batches/:id/download-url` only** | ❌ Path drift |
| `GET /api/calls?...` | `GET /api/calls?...` | ✅ Aligned (filter uses `agent_name`, frontend sends `agent_display_name` — re-check) |
| `GET /api/calls/agents` | `GET /api/calls/agents` | ✅ Aligned |
| `GET /api/calls/:id` | `GET /api/calls/:id` | ✅ Aligned |
| `DELETE /api/calls/:id` | **`PUT /api/calls/:id/hide` only** | ❌ Method drift |
| `GET /api/calls/:id/recording-url` | `GET /api/calls/:id/recording-url` | ⚠️ Returns raw path, not presigned S3 URL |
| `GET /api/voices` | `GET /api/voices` | ✅ Aligned |
| `POST /api/voices/lookup` | `POST /api/voices/lookup` | ⚠️ DB-only; ElevenLabs fallback TODO |
| `POST /api/voices/add` | `POST /api/voices/add` | ✅ Aligned |
| `POST /api/voices/:id/refresh` | `POST /api/voices/:id/refresh` | ⚠️ Returns cached DB row; does NOT call ElevenLabs |
| `DELETE /api/voices/:id` | `DELETE /api/voices/:id` | ✅ Aligned |
| `GET /api/voices/:id/agents` | `GET /api/voices/:id/agents` | ✅ Aligned |
| `POST /api/test-call/connect` | **Not in Lambda** | ❌ Missing (v2 has the closest equivalent; see Bucket C) |

### Endpoints v2 engine uses (via `lambda:Invoke`, not frontend)

- `GET /api/agents/:id/runtime-config` — agent config at call start
- `GET /api/phone-numbers/lookup?number=...` — inbound agent resolution
- `POST /api/calls` — call record UPSERT (twice per call)
- `POST /api/auto-actions` — post-call cost/score/actions

These are not relevant to frontend alignment but are listed for completeness.

---

## 5. Diff buckets

### BUCKET A — Aligned (frontend works against existing backend today)

Roughly **32 of 45** frontend endpoints work end-to-end against the shared Lambda as deployed. Specifically:

- All agent CRUD except draft/versions (`GET/POST/PUT/DELETE /api/agents[/:name]`, `/clone`, `/prompt`, `/agent-schema`)
- Phone numbers list, update, delete, search, buy
- Batch list, detail, download-url, draft delete, rows update, start/pause/resume/cancel/status
- Calls list, agents filter, detail
- Voices list, lookup, add, refresh, delete, voices-using

**Verification needed**: most "aligned" rows above are aligned in path + method. The exact response field-shape match should be verified by running the frontend against the shared Lambda once — there's a non-zero chance one or two responses use snake_case where the frontend expects camelCase or vice versa. The frontend's `unwrapAgentJson` already tolerates `{ agent }` / `{ data }` wrappers, which suggests some forgiveness is built in.

### BUCKET B — Shape mismatch (small adapter / patch)

These are real but small. Each is either a one-line fix on the frontend or a one-route add on the Lambda.

| # | Frontend expects | Lambda has | Recommended resolution | Effort |
|---|---|---|---|---|
| B1 | `GET /api/agents/:name/draft` | `GET /api/agent-drafts/:agent_id` | Add nested route to Lambda OR change frontend `getAgentDraft` URL | XS (~30 min) |
| B2 | `PUT /api/agents/:name/draft` | `PUT /api/agent-drafts/:agent_id` | Same as B1 | XS (with B1) |
| B3 | `GET /api/agents/:name/versions` | `GET /api/agent-versions/:agent_id` | Add nested route to Lambda OR change frontend | XS (~30 min) |
| B4 | `POST /api/agents/:name/versions` | `POST /api/agent-versions` | Same as B3 | XS (with B3) |
| B5 | `DELETE /api/calls/:id` | `PUT /api/calls/:id/hide` | Add DELETE handler on Lambda that calls the same hide logic, OR change frontend to PUT `/hide` | XS (~30 min). Recommend Lambda-side fix — DELETE is the conventional method |
| B6 | `GET /api/batches/:id/results` returning **binary blob** | `GET /api/batches/:id/download-url` returning `{ url }` | Add `/results` Lambda route that 302-redirects to the file (S3 presign) OR change frontend to fetch `download-url` then `window.open(url)` | S (~1 hr) |
| B7 | `POST /api/batches/upload` as **multipart/form-data** | Lambda expects `application/json` `{ rows }` | Add Busboy-style multipart parser in Lambda OR split into "frontend parses file, POSTs JSON" (less work but loses server-side validation) | M (~half day) |
| B8 | `GET /api/calls/agents` returns `{ agents }` with `display_name`/`agent_name` keys | Lambda returns `{ agents: [{ agent_name, display_name }] }` | Verify shape match by running the dashboard against the Lambda; either change one side if mismatch | XS (verify only) |
| B9 | Calls filter uses query param `agent_display_name` | Lambda accepts `agent_name` | Add a second query alias in the Lambda call list handler OR change frontend query key | XS (~15 min) |
| B10 | Recording URL is expected to be playable in `<audio>` | Lambda returns raw S3 path (e.g. `s3://bucket/key.wav`), not a presigned URL | Add S3 presign on Lambda `/recording-url` (Lambda already has S3 IAM) | S (~1 hr) |
| B11 | Voice "refresh" expected to re-fetch from ElevenLabs | Lambda returns cached DB row | Add an ElevenLabs API call in `/voices/:id/refresh` (also `/voices/sync` is a stub) | S (~1 hr) |
| B12 | `POST /api/phone-numbers/sync-twilio` expected to do something | Lambda returns `{total:0}` stub | Either drop Twilio sync UI button (frontend) or implement (Lambda). Given Daily-only strategy, recommend drop | XS (frontend) or M (Lambda) |

**Total Bucket B effort**: ~1 day of Lambda + frontend work to clear all 12 items.

### BUCKET C — Missing on both backends

| # | Frontend feature | What it needs | Recommended action | Effort |
|---|---|---|---|---|
| **C1** | **Test-call panel (`POST /api/test-call/connect` via Pipecat WebRTC)** | The frontend's `src/components/test-call-panel.tsx` and `test-call-modal.tsx` use the Pipecat client library (`@pipecat-ai/client-js` + `SmallWebRTCTransport`) to open a WebRTC session. Neither the shared Lambda nor v2 implements a Pipecat connect endpoint. | **Migrate the test-call panel to v2's `POST /start { direction: "browser" }` + Daily Web SDK.** Steps: (1) frontend calls `POST /start` with `X-API-Key` and gets `{ room_url, viewer_token }`, (2) frontend uses `@daily-co/daily-js` to join the Daily room with the viewer token, (3) frontend listens to Daily's data-message events for transcripts (we'd need to confirm v2 emits transcript events on the data channel, OR use a separate streaming channel like a presigned WebSocket). | **M (~2-3 days)** depending on transcript-streaming approach |
| C2 | Recording presigned URLs | Lambda returns raw S3 paths in `/recording-url` and `/batches/:id/download-url` | See B10 (recording) and B6 (batch results) — same root issue | (counted in B) |
| C3 | Multipart batch upload parser | Lambda expects JSON | See B7 | (counted in B) |
| C4 | A real Twilio sync | Stub only | See B12 — recommend drop Twilio, Daily-only strategy | (counted in B) |
| C5 | Phone-numbers manual-add (`POST /api/phone-numbers`) | Lambda has it; frontend exports but no UI binding | Decide: (a) leave it as dead code; (b) wire up a manual-add button in the UI; (c) remove from `api.ts`. Recommend (a) — zero work, no production impact | XS |
| C6 | Polling batch status (`GET /api/batches/:id/status`) | Lambda has it; frontend only uses it in the orphan `NewBatchModal` component that isn't mounted | Either delete the modal (frontend) or wire it up properly. Recommend delete | XS |

**C1 is the only genuinely-large piece of work.** Everything else is bookkeeping.

#### Architecturally surprising — flag for separate discussion

The C1 test-call mismatch is the only architectural surprise. Three subtleties:

1. **v2 doesn't emit transcripts on Daily's data channel today.** Looking at v2's `runtime/bot.py`, the transcript accumulator only writes to the call record at `finalize_call` (post-call). For a live test-call panel that shows transcripts in real-time, we'd need to add a transcript-message broadcast to the data channel (small change to `app/bot/bot.py`, maybe ~50 lines).
2. **`viewer_token`** has limited scope (joiner only, no admin). That matches the frontend's needs but may not support test scenarios like "send DTMF" or "interrupt" buttons in the test panel. If those are required, we'd need to issue a higher-permission token. Check the test-call panel UI for required interactions.
3. **The frontend's test-call panel sends `use_draft: true`** — v2 doesn't have a draft concept (drafts live in the Lambda). Either v2's `/start` needs to be taught to fetch the draft config instead of the live config (clean), or the frontend has to send the entire draft config in the body (less clean — exposes config to client). Recommend v2 changes: `POST /start { direction: "browser", use_draft: true }` triggers a fetch of `voice_agent_drafts.config_snapshot` instead of `voice_agents`.

---

## 6. API layer assessment (frontend)

| Question | Answer |
|---|---|
| Centralized? | Yes — `src/lib/api.ts`, ~548 lines, 44 named exports. One exception: `test-call-panel.tsx` duplicates `API_BASE` and uses Pipecat client (not `fetch`). |
| Base URL config | `process.env.NEXT_PUBLIC_API_BASE_URL` (build-time, browser-visible). Default `http://localhost:8000`. No Next.js rewrites. |
| Auth | Static `X-API-Key: ${NEXT_PUBLIC_COSENTUS_API_KEY}` header on every `api.ts` call. **Test call panel does NOT send it** — gap. |
| Error handling | Mix of `throw new Error(...)`, FastAPI `detail` parser (`throwFromApiBody`), and silent fallbacks. UI shows `sonner` toasts; some pages swallow errors. No global interceptor, no retry layer. |
| Centralization quality | High. Switching the backend base URL is a one-env-var change. **Switching individual endpoint paths is also a one-line-per-endpoint change** in `api.ts`. This makes Phase 1 cheap. |
| Surprising | (1) API key is `NEXT_PUBLIC_*` — exposed in the browser bundle. Anyone who loads the dashboard sees it. (2) Test-call panel doesn't send the API key but the rest of the app does. (3) Some response unwrapping (`unwrapAgentJson`) silently accepts both `{ agent }` and `{ data }` wrappers, hinting at past contract drift. |

### Practical implication

Because `api.ts` is so centralized, **most of Bucket B can be fixed entirely on the frontend** (change a path string in `api.ts`, no backend changes) OR entirely on the Lambda (add a route alias, no frontend changes). The choice between "fix in frontend" vs "fix in Lambda" should be driven by which fix is more durable. Most of the time, fixing in Lambda is better because:

- We can deploy the Lambda independently of the frontend.
- Other future clients (CLI tools, internal admin scripts) benefit from canonical paths.
- The frontend's nested paths (`/api/agents/:name/draft`) are more RESTful than the Lambda's current flat paths (`/api/agent-drafts/:id`).

**Recommendation**: clear Bucket B by adding route aliases in the Lambda. ~1 day total.

---

## 7. Recommended phasing + effort

### Phase 1 — Drift cleanup (~1 day)

**Goal**: Get the dashboard working end-to-end against the existing deployed Lambda, no v2 changes.

| Task | Where | Effort |
|---|---|---|
| Add `GET/PUT /api/agents/:name/draft` route alias on Lambda → `voice_agent_drafts` | Lambda | XS |
| Add `GET/POST /api/agents/:name/versions` route alias on Lambda → `voice_agent_versions` | Lambda | XS |
| Add `DELETE /api/calls/:id` handler on Lambda (call same hide logic) | Lambda | XS |
| Add `GET /api/batches/:id/results` returning a 302 redirect to a presigned S3 URL | Lambda | S |
| Add multipart parser to `POST /api/batches/upload` (Busboy or similar) | Lambda | M |
| Add ElevenLabs API client to `POST /api/voices/sync` and `/voices/:id/refresh` | Lambda | S |
| Add S3 presign to `GET /api/calls/:id/recording-url` | Lambda | S |
| Add `agent_display_name` query alias to `/api/calls` filter (or change frontend query key) | Lambda or frontend | XS |
| Drop "Sync from Twilio" button OR implement | Frontend or Lambda | XS or M |

**Result**: ~95% of the dashboard works against the shared Lambda. The test-call panel still fails (Phase 2 unblocks that).

### Phase 2 — Test-call migration to v2 (~2-3 days)

**Goal**: The dashboard's test-call panel uses v2's `POST /start { direction: "browser" }` + Daily Web SDK.

| Task | Where | Effort |
|---|---|---|
| Add `@daily-co/daily-js` to frontend dependencies | Frontend | XS |
| Refactor `test-call-panel.tsx` to use Daily SDK instead of Pipecat client | Frontend | M |
| Refactor `test-call-modal.tsx` similarly | Frontend | S |
| Add `X-API-Key` header to the new connect path | Frontend | XS |
| Teach v2 `/start` to honor `use_draft: true` (fetch from `voice_agent_drafts` instead of live config when set) | v2 + Lambda | S |
| Add live-transcript broadcast on Daily data channel from v2 `bot.py` | v2 | S |
| End-to-end smoke test: dashboard test-call → v2 staging → Daily room → audio/transcript both ways | Manual | S |

**Result**: Test-call panel works on the new architecture. Frontend now genuinely depends on v2.

### Phase 3 — Polish + hardening (~2-3 days, optional)

| Task | Where | Effort |
|---|---|---|
| Move `X-API-Key` out of `NEXT_PUBLIC_*` (it's exposed today). Add a Next.js BFF layer (`app/api/proxy/*`) that holds the key server-side and forwards to the Lambda. Frontend `api.ts` calls `/api/proxy/*` instead. Keeps the API key off the client bundle. | Frontend | M |
| Test-call panel — make API key optional / move auth into the room token | Frontend | S |
| Promote `@aws-sdk/client-sqs` to `dependencies` in the Lambda (currently in devDependencies — production zip may be missing it) | Lambda | XS |
| Add API Gateway-level auth (API Key resource policy or WAF rule) so the Lambda isn't world-open | Lambda infra | M |
| Drop dead `api.ts` exports (`createPhoneNumber`, `syncVoices`, `NewBatchModal`) | Frontend | XS |

### Phasing summary

- **Phase 1 unblocks the dashboard against existing prod Lambda** — fastest path to "the UI works again."
- **Phase 2 is the actual frontend↔v2 integration** — only needed for the test-call feature.
- **Phase 3 is hardening** — schedule independently.

### Total effort

| Phase | Time | Risk |
|---|---|---|
| P1 | 1 day | Low (route aliases only) |
| P2 | 2-3 days | Medium (transcript streaming is the new ground) |
| P3 | 2-3 days | Low (operational hygiene) |
| **Sum** | **5-7 days** | |

If only P1 + P2 are done, dashboard is fully functional in 3-4 days.

---

## 8. Architectural flags (separate from buckets)

1. **The "v1 is dead" framing is partially wrong.** The shared Lambda is alive and used by v2. Only the v1 engine fork is dead. Any planning that assumes the Lambda needs to be replaced is overscoping. Recommend keeping the Lambda for the foreseeable future and only adding/correcting routes.
2. **The frontend's API key is browser-visible** (`NEXT_PUBLIC_COSENTUS_API_KEY`). Anyone who loads the dashboard can extract it and call the API directly. Combined with the Lambda not actually validating the key, the REST surface is effectively open. This is not a Phase 1 / Phase 2 blocker, but should be in the post-Layer-12 hardening backlog (Phase 3 + API Gateway-level enforcement).
3. **Recording paths are stored as raw S3 URIs, not presigned URLs.** The frontend's audio player can't actually load them today as-is — they'd fail with CORS / 403 against S3. The fix is one-side: add presigning in the Lambda. The fact that the frontend was historically built for this and presumably worked at some point suggests an old proxy layer was deleted along with v1.
4. **Pipecat WebRTC is dead in v2.** Anyone reading the frontend `test-call-panel.tsx` would assume v2 ships a Pipecat connect endpoint. It does not — and won't. The test-call code needs to be migrated to Daily. This is the only deep architectural change required.
5. **Drafts live in the Lambda, not v2.** v2's `/start` reads the LIVE agent config. To support "test call against draft," v2 needs to learn to read drafts. The cleanest spot is in v2's `app/config/agent_config.py::load_agent_config()` — branch on a `use_draft` flag and call the Lambda's `GetAgentDraft` operation instead of `GetRuntimeConfig`. Small ~50-line addition.
6. **Multipart upload missing.** Frontend uses `FormData`; Lambda only reads JSON bodies. This is a hard failure today for the batch upload flow.
7. **Cold-start side effects in Lambda.** `ensureTables()` runs `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE` on every cold start. Mostly harmless but worth knowing — production schema migrations happen implicitly on deploy.
8. **No dashboard health check.** The frontend doesn't poll a `/health` endpoint anywhere. If the backend is down, the user finds out by clicking something. Could be a Phase 3 polish item (low priority).
9. **The shared Lambda has many tier-2 RCM endpoints** (payers, claim-context, ROI, denial-patterns, call-templates, inbound-routes) that the dashboard does not consume. These are presumably used by something else (back-office tools? Cindy widget? scripts?). Worth confirming with Alex whether any are still needed; if not, dropping them simplifies the Lambda.

---

## 9. Open questions for Alex

1. **Test-call live transcripts**: does the dashboard need real-time transcripts during a test call, or is post-call sufficient? (Affects Phase 2 scope.)
2. **Test-call interactions**: does the dashboard need send-DTMF / interrupt / mute buttons during a test call? (Affects what room token permissions we mint.)
3. **Drafts vs live for test calls**: is "test the draft" essential, or would "test the live config" be acceptable for v1 of the migration? (Affects Phase 2 — the draft path is the more involved change.)
4. **Twilio sync**: keep the UI button and implement, or drop it permanently? Current strategy is Daily-only.
5. **`api.ts` dead exports**: leave as-is, or clean up? (`createPhoneNumber`, `syncVoices`, `NewBatchModal`.)
6. **Frontend BFF** (Phase 3): worth doing now to get the API key off the client, or defer until after Layer 12 cutover?
7. **Tier-2 RCM endpoints** in the Lambda — any of those consumed by something outside the dashboard? If not, candidate for cleanup.

---

## Appendix: full endpoint counts

| Source | Endpoint count |
|---|---|
| Frontend (`api.ts` + test-call panel) | 45 |
| Shared Lambda (all routes including internal/admin) | ~50+ (44 in frontend's path namespace + 6 internal RCM + 4 v2-runtime-only) |
| v2 engine (aiohttp) | 5 |

Of the 45 frontend endpoints:

- **32** are aligned with the Lambda as-is
- **12** have shape/path/method drift (Bucket B) — total ~1 day to fix
- **1** is genuinely missing in the new architecture (Bucket C: test-call) — ~2-3 days to migrate to v2

This audit confirms that the frontend↔backend migration is **mostly drift cleanup against the shared Lambda**, not a v2 rewrite. The frontend was never built against v2; it's built against the shared Lambda. The single new integration with v2 is the test-call panel, which is a self-contained component.
