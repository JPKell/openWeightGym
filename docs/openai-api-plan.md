# OpenAI-compatible API completion plan

Implementation status: see [R1](openai-api-work.md#r1-responses-streaming), [R3](openai-api-work.md#r3-stored-responses-api-and-console-page), and [R4](openai-api-work.md#r4-responses-in-batches) in the brief. TA is implemented by the [shared permission check](../loadcoach/request_tools.py); TB function tools use the [Responses adapter](../turnstile/responses.py). TC uses the [shared caller-key tool loop](../turnstile/tool_loop.py) and [MCP Responses adapter](../turnstile/response_tools.py), including approvals, exact results, sensitivity and editable limits. W1 adds the [Whisper engine and installer](../engines/whisper_cpp/); native/container qualification and pending deployment activation are recorded in [Installation](installation.md). T1 adds [speech translation](../engines/whisper_cpp/runtime.py) through ordinary multipart admission and Transcribe controls. K1 adds [reranker admission](../loadcoach/rerank.py), explicit model mode and the [verified live models](openai-api-work.md#k1-reranker-models-and-v1rerank). TD adds [Tool chat](../weightroom/tool_chat.py) with shared chat components, exact tool history, explicit approvals and portable private storage.

Drafted **2026-11-13**. Covers the endpoints judged worth adding or easy to add. Moderations is
held for an owner decision (see [Open decision](#open-decision-moderations)). Out of scope:
Assistants/Threads, vector stores, Realtime, fine-tuning, evals, containers, videos, image
variations, uploads and stored chat completions.

The implementation brief for the non-tool milestones (R1, R3, R4, a new whisper.cpp engine W1,
T1, K1), with the owner's interview decisions, is [openai-api-work.md](openai-api-work.md). Approved
tool work (TA–TD) is specified in [openai-tools-work.md](openai-tools-work.md). These briefs are
authoritative where they differ from the early design notes below.

Work through the milestones in order. Each one ships with tests, updates
[Docs → API → OpenAI](../weightroom/pages/docs/api/openai.html) and is deployed only on the
owner's request through `owg update; owg restart`, then verified live.

## Ground rules

- **One enforcement point.** Responses stays an adapter that translates to ordinary
  `/v1/chat/completions` through normal admission ([turnstile/responses.py](../turnstile/responses.py)).
  Model allowlists, sensitivity, hosted-offload rules, usage, logs and cancellation therefore
  stay where they are now. No endpoint gets its own scheduler, accounting or permission path.
- **Permissions are per account.** `use-tools` controls tool definitions and history in model
  requests. ToolLocker group/account patterns independently control visible and callable tools.
  App sessions use both accounts' intersection. A key requiring other permissions needs its own
  program account. See the [tool brief](openai-tools-work.md#ta-the-use-tools-ability).
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

## TA–TD. Tool use

The [tool brief](openai-tools-work.md) specifies the implemented `use-tools` permission,
client-defined function tools, server-run ToolLocker tools, approvals and the separate Tool chat
workspace. Its caller-key, sensitivity and bounded-loop contracts apply to both API and console.

## R3. Stored Responses

**Value:** medium. Many clients send `store:true` (OpenAI's default) or chain with
`previous_response_id`.

- Default to `store:true`. Persist the input items, output and settings in a new table in the front
  store, keyed by response ID and immutable owner identity, with the request's sensitivity level.
- Add `GET /v1/responses/{id}`, `DELETE /v1/responses/{id}` and `GET /v1/responses/{id}/input_items`
  (with pagination). Owners can read/delete; `see-log` permits reading others and `manage-users`
  permits deletion, always within current sensitivity clearance. Hidden records return the same 404.
- `previous_response_id` rebuilds the conversation from stored items. Refuse it if the earlier
  response belongs to another owner or exceeds the caller's current clearance.
- Refuse `store:true` together with `X-LoadCoach-Log: off`. Set a retention limit (a setting,
  default 30 days) and add a cleanup step. Account deletion removes the account's stored
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

## W1. whisper.cpp engine

The [Whisper brief](openai-api-work.md#w1-whispercpp-engine) covers the installer, native adapter,
model setup and verification. [Installation](installation.md) records completed native/container
qualification and the live checks awaiting authorized application deployment.

## T1. Audio translations

**Value:** medium for multilingual voice users; low effort.

- Add `POST /v1/audio/translations` (multipart, the transcription fields without `language`),
  returning English text in the requested `response_format`.
- Add a per-model capability, `translates`, declared by the transcription engine family. Only
  families whose native runtime supports translation set it (Whisper-family models do; many
  others do not). Refuse other models with a clear 400 rather than returning a transcript.
- Reuse the front-side multipart conversion in [turnstile/app.py](../turnstile/app.py) and the
  Whisper transcription adapter ([engines/whisper_cpp/runtime.py](../engines/whisper_cpp/runtime.py)).
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

## Milestones

| Milestone | Size | Depends on |
|---|---|---|
| R1 Responses streaming | M | — |
| R3 Stored Responses and console | L | R1 |
| R4 Responses in batches | S | R1, R3 |
| W1 whisper.cpp engine and installer | L | — |
| T1 Audio translations | S | W1 |
| K1 Rerank | M | — |
| TA use-tools ability | M | — |
| TB Function tools | M | R1, TA |
| TC ToolLocker MCP loop | L | TA, TB, R3 |
| TD Tool chat | L | TA, TC |

The briefs specify milestone order and reporting. Deploy only on the owner's request.

## Open decision: moderations

`/v1/moderations` was held pending the owner's decision on whether any client needs it. If it is
approved, it would route to a permitted Classifier model and return the OpenAI shape (`flagged`,
`categories`, `category_scores`), with scores labelled as uncalibrated.
