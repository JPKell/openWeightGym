# OpenAI-compatible API completion plan

Implementation status: see [R1](openai-api-work.md#r1-responses-streaming), [R3](openai-api-work.md#r3-stored-responses-api-and-console-page), and [R4](openai-api-work.md#r4-responses-in-batches) in the brief. TA is implemented by the [shared permission check](../loadcoach/request_tools.py); TB function tools use the [Responses adapter](../turnstile/responses.py). TC uses the [shared caller-key tool loop](../turnstile/tool_loop.py) and [MCP Responses adapter](../turnstile/response_tools.py), including approvals, exact results, sensitivity and editable limits. W1 adds the [Whisper engine and installer](../engines/whisper_cpp/); native/container qualification and pending deployment activation are recorded in [Installation](installation.md). T1 adds [speech translation](../engines/whisper_cpp/runtime.py) through ordinary multipart admission and Transcribe controls. K1 adds [reranker admission](../loadcoach/rerank.py), explicit model mode and the [verified live models](openai-api-work.md#k1-reranker-models-and-v1rerank). TD adds [Tool chat](../weightroom/tool_chat.py) with shared chat components, exact tool history, explicit approvals and portable private storage.

Drafted **2026-11-13**; decisions settled with the owner on 2026-11-13 and 2026-11-14. This page is
the overview. The two briefs are the authoritative specifications:

- [openai-api-work.md](openai-api-work.md): Responses streaming, stored Responses and their console
  page, Responses in batches, a whisper.cpp engine, audio translations, rerank.
- [openai-tools-work.md](openai-tools-work.md): the `use-tools` ability, function tools in
  Responses, server-run ToolLocker tools, and the Tool chat page.

Where this page and a brief differ, the brief wins. Update both together.

## Scope

**In scope:** the milestones below.

**Held for an owner decision:** `/v1/moderations` (see [Open decision](#open-decision-moderations)).

**Out of scope:**
- Assistants/Threads, vector stores, Realtime, fine-tuning, evals, containers, videos, image
  variations, uploads and stored chat completions;
- any MCP server other than this the control node's own ToolLocker;
- per-API-key permissions.

## Ground rules

- **One enforcement path.** Responses stays an adapter that translates to ordinary
  `/v1/chat/completions` through normal admission ([turnstile/responses.py](../turnstile/responses.py)).
  Model allowlists, sensitivity, hosted-offload rules, usage, logs and cancellation stay where
  they are. No endpoint gets its own scheduler, accounting or permission path.
- **Permissions are per account.** Group settings with per-account overrides, and `limited()` for
  app-made sessions ([loadcoach/groups.py](../loadcoach/groups.py)), decide what an account may do.
  A key that needs different rules gets its own program account.
- **Two separate tool controls:**
  - the new `use-tools` ability decides whether an account may send tools to *any* model it routes,
    local or cloud;
  - the existing `tools` patterns decide which ToolLocker tools it sees and may call
    (`may_see_tool`, checked in [toollocker/door.py](../toollocker/door.py)).
- **Tool results follow sensitivity levels.** A sensitive result raises the trace's level, and
  hosted models are then refused.
- Unknown parameters keep returning a clear 400 rather than being silently ignored.
- Requests sent with `X-LoadCoach-Log: off` never create stored state.
- Deployment only on the owner's request, through `owg update; owg restart`, then verified live.

## Milestones

| # | Milestone | Brief | Size | Depends on |
|---|---|---|---|---|
| R1 | Responses streaming (text, reasoning, refusals) | api-work | M | — |
| R3 | Stored Responses, `previous_response_id`, Logs → Responses page | api-work | L | R1 |
| R4 | Responses in batches (stateless) | api-work | S | R1, R3 |
| W1 | whisper.cpp engine across installer, components and engine pages | api-work | L | — |
| T1 | `/v1/audio/translations` | api-work | S | W1 |
| K1 | Reranker models and `/v1/rerank` | api-work | M | — |
| TA | `use-tools` ability, enforced on every model entry point | tools-work | M | — |
| TB | Function tools in Responses | tools-work | M | R1, TA |
| TC | Shared tool loop; server-run ToolLocker tools in Responses | tools-work | L | TA, TB, R3 |
| TD | Tool chat page | tools-work | L | TA, TC |

**Suggested overall order:** R1 → R3 → R4 → TA → TB → TC → TD → W1 → T1 → K1.
- TA, W1 and K1 have no dependencies and can move earlier.
- If the owner sends only one brief, its own order applies.

## Why these

- **Responses streaming and tools** unblock agent clients: the Agents SDK, Codex CLI and current SDKs
  default to streamed Responses with tools.
- **Stored Responses** support clients that rely on OpenAI's default `store:true` and on chaining.
- **whisper.cpp** adds speech translation and wide language coverage that the installed audio.cpp
  families lack.
- **Rerank** improves retrieval for RAG clients, and later possibly Collections.
- **Tool chat** lets people see and call the tools they're allowed, directly or through a model,
  with each model call approved.

## Open decision: moderations

`/v1/moderations` is used by public-facing apps to screen user input and model output. It was held
because a private suite where everyone has an account rarely needs it. If approved, it would route
to a permitted Classifier model (for example Llama Guard or ShieldGemma) and return the OpenAI
shape (`flagged`, `categories`, `category_scores`), with scores labelled as uncalibrated.
