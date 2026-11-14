# OpenAI-compatible API completion plan

Implementation status: see [R1](openai-api-work.md#r1-responses-streaming), [R3](openai-api-work.md#r3-stored-responses-api-and-console-page), and [R4](openai-api-work.md#r4-responses-in-batches) in the brief. TA is implemented by the [shared permission check](../loadcoach/request_tools.py); TB function tools use the [Responses adapter](../turnstile/responses.py).

Drafted **2026-11-13**. Covers the endpoints judged worth adding or easy to add. Moderations is
held for an owner decision (see [Open decision](#open-decision-moderations)). Out of scope:
Assistants/Threads, vector stores, Realtime, fine-tuning, evals, containers, videos, image
variations, uploads and stored chat completions.

The implementation brief for the non-tool milestones (R1, R3, R4, a new whisper.cpp engine W1,
T1, K1), with the owner's interview decisions, is [openai-api-work.md](openai-api-work.md). Tool
work (R2) is still being reviewed.

Work through the milestones in order. Each one ships with tests, updates
[Docs → API → OpenAI](../weightroom/pages/docs/api/openai.html) and is deployed only on the
owner's request through `owg update; owg restart`, then verified live.

## Ground rules

- **One enforcement point.** Responses stays an adapter that translates to ordinary
  `/v1/chat/completions` through normal admission ([turnstile/responses.py](../turnstile/responses.py)).
  Model allowlists, sensitivity, hosted-offload rules, usage, logs and cancellation therefore
  stay where they are now. No endpoint gets its own scheduler, accounting or permission path.
- **Tool permissions keep working per account and per API key.** Today a group's `tools`
  patterns, each account's `add_tools`/`remove_tools` and `limited()` for app-made sessions
  ([loadcoach/groups.py](../loadcoach/groups.py)) decide which ToolLocker tools are shown and callable
  (`may_see_tool`, checked in [toollocker/door.py](../toollocker/door.py)). Program accounts are the
  per-key boundary: each API key belongs to an account with its own overrides. Every tool path in
  this plan must go through those checks; see [R2](#r2-responses-tools).
- Unknown parameters keep returning a clear 400 rather than being silently ignored.
- Requests sent with `X-LoadCoach-Log: off` must never create stored state.

## R1. Responses streaming

**Value:** high. Agents SDK, Codex CLI and current SDKs default to streamed Responses.

- Accept `stream:true`. Stream the internal chat completion and translate its SSE chunks into
  Responses events: `response.created`, `response.in_progress`, `response.output_item.added`,
  `response.content_part.added`, `response.output_text.delta`/`.done`, `response.output_item.done`,
  `response.completed`, or `response.failed`/`response.incomplete`, with the correct
  `sequence_number`.
- Map reasoning content to `reasoning` output items when the model emits it.
- Report usage in `response.completed` from the chat usage chunk (`stream_options.include_usage`).
- If the client disconnects, cancel through the existing chat request so the model slot is
  released. Keep forwarding `x-loadcoach-*` response headers.
- **Tests:** event order and shape against recorded fixtures; disconnect cancels; errors before
  and during the stream; usage totals match the request log.

## R2. Responses tools

**Value:** high. Agent clients send tools on almost every request.

### R2a. Client-defined function tools

- Accept `tools` entries of `type: "function"`, plus `tool_choice` and `parallel_tool_calls`.
  Translate them to chat `tools`.
- Map assistant `tool_calls` to `function_call` output items. Map `function_call_output` input
  items back to `role: tool` messages. Streaming emits `response.function_call_arguments.delta`
  and `.done`.
- Refuse tools for models whose catalogue entry has `tools: false`, using the same check chat uses
  ([loadcoach/app.py](../loadcoach/app.py) `takes_tools` / template caps).

The client executes these functions, not the server. Restricting them protects model use and
cost, not data. For the per-account/key control:

- Add an ability, e.g. `define-tools`, granted to groups by default, that permits sending
  `tools` at all. Enforce it in LoadCoach `forward`. Chat and Responses then share one
  check, and `limited()` already intersects abilities for app-made sessions.
- Optionally filter declared function names against the account's tool patterns. This should
  be decided when the work starts: function names are client-chosen, so name patterns are a
  weak control.

### R2b. Server-run ToolLocker tools (`type: "mcp"`)

This path enforces tool permissions in a meaningful way, because the server runs the tools.

- Accept `{"type":"mcp","server_label":"toollocker","server_url":<this front's /mcp[/toolset]>,
  "allowed_tools":[...], "require_approval": "never" | {...}}`. Refuse any other `server_url`:
  calling arbitrary remote MCP servers is an egress path this plan does not open.
- On each request, list tools from ToolLocker **with the caller's own key**, so the tools offered
  are exactly what `may_see_tool`, the sensitivity level and toolsets allow for that account or
  key. `allowed_tools` can narrow that list further, never widen it. Emit `mcp_list_tools`.
- Run the loop on the front: model → `mcp_call` → ToolLocker `tools/call` (caller's key again, so
  the gateway re-checks permission on every call) → result back to the model. Bound the loop
  by iteration count, deadline and output size. Use the existing trace/sensitivity headers so
  every step appears in AI Logs.
- When `require_approval` applies, return `mcp_approval_request` and continue on
  `mcp_approval_response`. This needs R3 (`previous_response_id`).
- Reuse the WeightRoom MCP client in [weightroom/app.py](../weightroom/app.py) instead of a new one.
- **Tests:** two accounts with different `tools`/`remove_tools` see different `mcp_list_tools`;
  an app-made key is limited to the intersection; a tool hidden mid-run is refused at call time;
  a foreign `server_url` is refused; loop limits; streaming events for MCP items.

## R3. Stored Responses

**Value:** medium. Many clients send `store:true` (OpenAI's default) or chain with
`previous_response_id`.

- Allow `store:true`. Persist the input items, output and settings in a new table in the front
  store, keyed by response ID and immutable owner identity, with the request's sensitivity level.
- Add `GET /v1/responses/{id}`, `DELETE /v1/responses/{id}` and `GET /v1/responses/{id}/input_items`
  (with pagination). Only the owner can read these, and only at their current sensitivity clearance.
  Others get the same 404 as for a missing record.
- `previous_response_id` rebuilds the conversation from stored items. Refuse it if the earlier
  response belongs to another owner or exceeds the caller's current clearance.
- Refuse `store:true` together with `X-LoadCoach-Log: off`. Set a retention limit (a setting,
  default e.g. 30 days) and add a cleanup step. Account deletion removes the account's stored
  responses through the existing account cleanup hook. Include the table in backup, restore and
  verified transfer.
- `background:true` is out of scope for this milestone. Refuse it explicitly.
- **Tests:** round trip, ownership isolation, sensitivity reduction, chaining, retention cleanup,
  deletion, PostgreSQL migration.

## R4. Responses in batches

**Value:** low effort once R1–R3 exist.

- Add `/v1/responses` to `ENDPOINTS` in [loadcoach/batches.py](../loadcoach/batches.py), stateless
  only (`store:false`, no `previous_response_id`, no MCP tools in the first version).
- **Tests:** a mixed batch file; refusing stored or MCP lines with per-line errors.

## T1. Audio translations

**Value:** medium for multilingual voice users; low effort.

- Add `POST /v1/audio/translations` (multipart, the transcription fields without `language`),
  returning English text in the requested `response_format`.
- Add a per-model capability, `translates`, declared by the transcription engine family. Only
  families whose native runtime supports translation set it (Whisper-family models do; many
  others do not). Refuse other models with a clear 400 rather than returning a transcript.
- Reuse the front-side multipart conversion in [turnstile/app.py](../turnstile/app.py) and the
  Soundroom transcription route ([soundroom/runtime.py](../soundroom/runtime.py)).
- **Tests:** multipart request; capability refusal; every response format.

## K1. Reranker models and `/v1/rerank`

**Value:** medium. RAG clients use it, and Collections could use it to improve retrieval.

- Add a `reranker` model type: llama.cpp started in reranking mode (`--reranking`). Add the type
  to the Models add form, the shared `model_type_choices` pills and the catalogue, reusing the
  existing templates.
- Accept `POST /v1/rerank` (alias `/rerank`) in the Jina/Cohere shape (`model`, `query`,
  `documents`, `top_n`, `return_documents`) and forward it through ordinary admission. Refuse
  non-reranker models.
- Usage and logs as for embeddings. Add it to batch `ENDPOINTS`.
- **Follow-up (separate decision):** optional rerank step in Collections search.
- **Tests:** forwarding with a fake engine; type refusal; launch flags; model page UI checks
  per the UI audit in [test_ui_consistency.py](../tests/test_ui_consistency.py).

## Order and size

| # | Milestone | Size | Depends on |
|---|---|---|---|
| 1 | R1 Responses streaming | M | — |
| 2 | R2a Function tools + `define-tools` | M | R1 |
| 3 | R3 Stored Responses | M | — |
| 4 | R2b ToolLocker MCP tools | L | R1, R3 for approvals |
| 5 | R4 Responses in batches | S | R1–R3 |
| 6 | T1 Audio translations | S | — |
| 7 | K1 Rerank | M | — |

T1 and K1 are independent of the rest and can be done at any point.

## Open decision: moderations

`/v1/moderations` was held pending the owner's decision on whether any client needs it. If it is
approved, it would route to a permitted Classifier model and return the OpenAI shape (`flagged`,
`categories`, `category_scores`), with scores labelled as uncalibrated.
