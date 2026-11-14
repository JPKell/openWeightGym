# OpenAI-compatible API: implementation brief (non-tool work)

Written **2026-11-13** for the agent implementing it, after an interview with the owner on the
same day. Overview and rationale: [openai-api-plan.md](openai-api-plan.md). This brief covers
**R1, R3, R4, W1, T1 and K1**.

**Not in scope. Do not start these:**
- Any tool work: Responses `tools`, `tool_choice`, `function_call` items, MCP tools, the
  `define-tools` ability (R2a/R2b). The owner is still reviewing them.
- `/v1/moderations`: awaiting the owner's decision.
- Everything listed as out of scope in the plan.

Keep refusing tool parameters exactly as today.

## Owner decisions (2026-11-13)

| Question | Decision |
|---|---|
| `store` default | `true`, matching OpenAI |
| Retention of stored responses | 30 days, adjustable by an administrator |
| Stored responses in the console | A new page, beside Requests and Batches under Logs → Model activity |
| Reading other users' stored responses | Allowed with the existing `see-log` ability, up to the reader's sensitivity clearance |
| Deleting other users' stored responses | Allowed with `manage-users`, up to the reader's clearance, with two-click confirmation |
| Whisper | Add **whisper.cpp** as a new engine (W1) **before** translations. T1 uses Whisper |
| Whisper model | `large-v3`, which the owner is downloading. Ask the owner for its path |
| Reasoning-model test | `qwen3.6-35b-a3b-q4` |
| Rerankers to install | `bge-reranker-v2-m3` and `Qwen3-Reranker-4B` on this workstation, **jpk-main** (10.77.10.84) |
| Git | One commit per milestone, pushed to `main` once the milestone is finished. **No deployment** |

## Before you start

- Read [AGENTS.md](../AGENTS.md). Its Web UI rules and template index apply to the stored-responses
  page, W1's engine UI and K1's model-type UI. Its deployment rules apply throughout.
- **Do not deploy** unless the owner asks. When asked, use `owg update; owg restart` on the front
  (`ssh 10.77.10.42 '~/.local/bin/owg --plain --yes update; ~/.local/bin/owg --plain --yes restart'`)
  and on each affected node, then verify live behaviour.
- **Model downloads and model setup** for the rerankers on jpk-main are approved. Installing a
  native runtime on a machine (the W1 whisper.cpp engine) changes that node, so ask the owner
  before doing it outside a test container.
- The working tree may contain other people's uncommitted changes. Stage only the files you
  changed. For a file that also has someone else's edits, stage only your hunks. Commit and push
  each milestone separately.
- Run the affected test files with `.venv/bin/python -m pytest`. Some tests already fail on a clean
  HEAD. Compare against HEAD before blaming your change, and report pre-existing failures
  separately.
- When something below proves false (a native runtime lacks a feature, a contract differs),
  stop that milestone and report it. Do not invent a workaround.

## Shared rules for every milestone

1. **Use the normal admission path.** Responses work goes through `service.door` to
   `/v1/chat/completions`, as [turnstile/responses.py](../turnstile/responses.py) does now. Other
   new endpoints go through the existing turnstile `pass_on` → LoadCoach `forward` path. Do not
   add a scheduler, accounting path, listener, service or credential.
2. **Owner identity** is `user.identity or user.name`, from the caller resolved from the bearer key
   (see [turnstile/extractions.py](../turnstile/extractions.py)). A record that is hidden or missing
   returns the same 404.
3. **Sensitivity.** Store `X-LoadCoach-Sensitivity` (default `normal`) with any record. Refuse
   reading a record whose level is above the caller's current `group.max_sensitivity`, using the
   same 404. Levels come from `LEVELS` in `loadcoach.groups`.
4. **`X-LoadCoach-Log: off`** requests must leave no stored state behind.
5. **Errors** use the existing shape `{"error": {"message", "type", "code"}}` with `PRIVATE`
   headers. Parameters that aren't supported keep returning 400 with `code: unsupported_parameter`.
6. **Docs:** update [docs/api/openai.html](../weightroom/pages/docs/api/openai.html) (it uses the
   `docs_parts.html` macros) and the relevant markdown docs in the same commit as the code. When you
   add or move shared UI components, update the AGENTS.md template index.

## R1. Responses streaming

**Implemented 2026-11-14:** shared streamed/non-streamed output builder, reasoning/refusal events, final usage, failure boundaries and disconnect cleanup. Real Qwen3.6 chat capture: request `1810895650947769`, fixture `tests/fixtures/responses/qwen3.6-35b-a3b-q4.sse` (length termination). Native usage omits a separate reasoning-token count; supplied counts are preserved without estimating.


**Today:** `chat_request` refuses `stream:true`, and `create` makes a single non-streaming
`door.post`.

**Build:**
- Allow `stream:true`. Send the chat request with `stream: true` and
  `stream_options: {"include_usage": true}`. Use
  `service.door.stream('POST', '/v1/chat/completions', ...)`, following the streaming pattern in
  `turnstile/extractions.py`.
- Return `text/event-stream`. Each event is `event: <type>\ndata: <json>\n\n`, and every payload
  carries `type` and a `sequence_number` that increases from 0. Event order for a text answer:
  1. `response.created` (`status: in_progress`, empty `output`)
  2. `response.in_progress`
  3. `response.output_item.added`: the `message` item, `status: in_progress`
  4. `response.content_part.added`: an `output_text` part with empty text
  5. `response.output_text.delta`, repeated (`item_id`, `output_index`, `content_index: 0`, `delta`)
  6. `response.output_text.done` (full text)
  7. `response.content_part.done`
  8. `response.output_item.done`
  9. a final event, `response.completed`, `response.incomplete` (`finish_reason: length` →
     `max_output_tokens`) or `response.failed`. The `response` object is the same shape
     `response_body` builds, including usage taken from the usage chunk.
- **Reasoning:** when chunks carry `delta.reasoning_content`, emit a `reasoning` output item before
  the message, using `response.output_item.added`, `response.reasoning_summary_part.added`,
  `response.reasoning_summary_text.delta`/`.done`, `response.reasoning_summary_part.done` and
  `response.output_item.done`. Count it in `output_tokens_details.reasoning_tokens`. The
  non-streaming path must return the same `reasoning` item.
  - Verify against **`qwen3.6-35b-a3b-q4`**: capture a real streamed chat completion from it
    through the existing live API, using a key the owner provides, and keep it as a test fixture.
    Do not deploy to test this.
- A `refusal` delta becomes a `refusal` content part (`response.refusal.delta`/`.done`).
- If the upstream returns a tool call, end with `response.failed`, code `unsupported_output`,
  just as the non-streaming path refuses it now.
- **Errors:**
  - Before the first byte, return the ordinary JSON error with its upstream status.
  - Mid-stream, emit `error` then `response.failed`, and close.
  - If the client disconnects, close the upstream stream so LoadCoach's normal cancellation frees
    the slot.
- Forward the `x-loadcoach-*` / `x-suite-*` response headers from the upstream reply.
- Refactor `response_body` so the streamed and non-streamed paths build the final object the same
  way, with one shared builder. R3 stores that object.

**Tests** (extend the existing Responses tests, `grep -rln responses tests/`):
- Exact event sequence and `sequence_number`s for recorded chat SSE fixtures, including the real
  Qwen3.6 reasoning capture.
- Usage taken from the final chunk.
- `length` gives `response.incomplete`.
- An error before the stream starts keeps its status; an error mid-stream gives `response.failed`.
- Disconnecting closes the upstream request (assert the fake upstream saw it close).
- Tool-call output gives `unsupported_output`.
- The official `openai` Python SDK `client.responses.create(stream=True)` parses the stream from
  the test app, if the SDK is in the venv. Otherwise skip with a reason.

## R3. Stored Responses, API and console page

**Implemented 2026-11-14:** default storage, private lifecycle and stable input-item paging, same-owner chains, live retention, quota checks, account cleanup and the Responses console ledger. Metadata stays unsupported. SQLite/PostgreSQL backups and verified transfer include the accounts-bound `turnstile.responses` schema; browser layout checks cover both widths and themes.


### Storage

- A new front-owned store following
  [weightroom/extraction_store.py](../weightroom/extraction_store.py). Register its migration in
  [db/schemas.py](../db/schemas.py) `STREAMS`, named to match where Turnstile keeps similar state,
  using portable SQLite/PostgreSQL SQL.
- One table `responses`:
  - `id`, `owner`, `owner_name` (for display; access uses `owner`), `level`, `created`, `expires`,
    `status`, `model`, `previous` (nullable `id`), `request_id` (the ordinary request record's ID,
    when known)
  - `request` JSON: the normalized input items and settings
  - `response` JSON: the final Responses object
  - indexes on `(owner, created)` and `(created)`
- Check that backup/restore and verified transfer find the new store the way they find the
  extraction store (`db/`). Add it wherever store lists are enumerated, including the DB
  operations desk inventory.

### Create

- `store` defaults to **`true`**. Saving happens after completion, for both streaming and
  non-streaming requests. Failed and incomplete responses are saved with their status.
- Refuse `store:true` with `X-LoadCoach-Log: off` (400). That includes the default case: a
  log-off request that omits `store` is refused with a message telling the client to send
  `store:false`.
- Echo `store` correctly in the object.

### API routes

Register these beside `create` in `routes()`.

| Route | Who | Returns |
|---|---|---|
| `GET /v1/responses/{id}` | owner, or `see-log` within clearance | the stored object |
| `GET /v1/responses/{id}/input_items` | same | a list object (`data`, `first_id`, `last_id`, `has_more`) with `limit` (1–100, default 20), `order` (`asc`/`desc`, default `desc`) and `after`. Input items get stable IDs (`msg_…`) when saved |
| `DELETE /v1/responses/{id}` | owner, or `manage-users` within clearance | `{"id", "object": "response.deleted", "deleted": true}` |

Anyone else gets 404, the same as for a missing record.

### `previous_response_id`

- Load the earlier record. It must belong to the **same owner** and be within current clearance,
  otherwise 404. `see-log` does not allow continuing someone else's chain.
- Rebuild the messages: earlier inputs + earlier output + new input. `instructions` are not
  carried over (OpenAI behaviour). Follow the chain at most 50 responses deep.
- The new record stores `previous` and only its own input. `previous_response_id` with
  `store:false` on the new request is allowed when the earlier one was stored.

### Retention and limits

- **30 days by default**, as a setting in the existing Turnstile/LoadCoach settings mechanism.
  It appears on the Settings pages through the existing setting rows, not a new form.
- Prune on startup and periodically, as extractions do.
- Per-owner cap: 10,000 records or 256 MiB, with a clear 409 when it's reached.
- **Account deletion:** add a `forget(owner)` and wire it into the account-cleanup hook the same
  way `forget_extractions` is in [turnstile/app.py](../turnstile/app.py).

### Still refused

`background:true`, `conversation`, `include`, and `truncation` other than `disabled`. You may
either refuse `metadata` or accept it bounded to 16 string pairs, stored and echoed. Document
whichever you choose.

### Console page: Logs → Model activity → Responses

- **Route** `/responses` with detail `/responses/{id}`. Add it to
  [weightroom/navigation.py](../weightroom/navigation.py) in `Logs` → `Model activity`, after
  Requests and Batches.
- **Data** comes from new front-side list/detail functions on the store. Do not add a new public
  API for listing; keep the list to the console. The list is scoped like Requests:
  - the caller's own responses by default;
  - with `see-log`, everyone's within the caller's clearance, with a user filter using the shared
    [filter_parts.html](../weightroom/pages/filter_parts.html) user/group filter.
- **Columns:** created, user, model, status, sensitivity, input/output tokens, preview of the first
  input text (escaped, truncated), chained (has `previous`).
  - Filters: user, model, status, time range, text search over the stored previews.
  - Server-side pagination using [pagination.html](../weightroom/pages/pagination.html).
- **Detail:**
  - the input items and output, with reasoning shown in a native disclosure;
  - settings and usage;
  - a link to the ordinary request record when `request_id` is known;
  - links to the previous and next responses in the chain;
  - a JSON export of the stored object.
- **Delete:** shown to the owner and to `manage-users` holders within clearance, using the shared
  two-click confirmation (`data-confirm-two`) from [parts.html](../weightroom/pages/parts.html).
- **Reuse** the Requests ledger components ([request_parts.html](../weightroom/pages/request_parts.html),
  [requests.css](../weightroom/static/requests.css)) and the shared `kpi_parts.html`, `parts.html`
  and `docs_parts.html` templates. Add a template only where a component is new, and record it in
  the AGENTS.md template index.
- Empty, error and narrow (390 px) states, light and dark.

### Tests

- Create → get → input_items paging → delete → 404.
- `store` defaults to true.
- Visibility:
  - A second account without `see-log` gets 404.
  - A `see-log` reader can read but not delete, and not above their clearance.
  - A `manage-users` reader can delete within clearance.
  - A lowered clearance hides records.
- Chaining:
  - A chain sends the right message sequence upstream.
  - A foreign `previous_response_id` gets 404, even for `see-log` readers.
- Log-off requests with `store` set or omitted are refused.
- Expiry prunes records; account deletion removes them.
- SQLite and PostgreSQL migrations (see `tests/test_postgresql.py`); backup/transfer includes the
  table (see `tests/test_database_operations.py`).
- Page tests:
  - scoping, filters, pagination, delete confirmation, escaping of stored text;
  - the UI audit and all-page browser checks in
    [tests/test_ui_consistency.py](../tests/test_ui_consistency.py), including narrow and empty
    states.

## R4. Responses in batches

**Implemented 2026-11-14:** stateless Responses lines run through the existing front adapter and admission path. Mixed chat/Responses files retain each line’s endpoint across restart and verified SQLite/PostgreSQL transfer. Stream parameters, storage and chains fail per line.

**Build:**
- Add `/v1/responses` to `ENDPOINTS` in [loadcoach/batches.py](../loadcoach/batches.py). Batches
  run on compute LoadCoach, but Responses lives in Turnstile. Trace how batch lines are executed
  (`turnstile/batches.py` `FrontBatches`, `loadcoach/batches.py`) and run each `/v1/responses`
  line through the Responses adapter (`chat_request` + the shared response builder), not as a raw
  chat line. If that isn't possible without a new path, stop and report.
- Batch lines are stateless. Treat a missing `store` as `false` for batch lines. Refuse a line
  with `stream`, `store:true` or `previous_response_id` as a per-line error in the error file.
  Don't fail the whole batch.

**Tests:** a mixed batch file with chat and responses lines; a refused stored or streaming line
appears in the error file; output lines have the Responses body shape.

## W1. whisper.cpp engine

**Goal:** add whisper.cpp as a new speech-to-text engine beside audio.cpp. It must be available
everywhere engines already appear: the installer, `owg` updates, components, the engine sections
and audio model setup. T1 then uses it for translation.

**Pattern:** follow the existing `audiocpp` engine end to end. Find every place it is wired with
`grep -rln audiocpp --include=*.py --include=*.sh --include=*.html .`. As of this writing that
covers:
- **Registry and adapter:** [engines/__init__.py](../engines/__init__.py) `_ENGINES` and
  `checked_installations`; a new adapter, e.g. `engines/whisper_cpp/`, modelled on
  [soundroom/engine.py](../soundroom/engine.py). It needs a `capabilities()` that sets `audio`,
  `transcription` and a new `translates` mark, and refuses chat, embeddings and images.
- **Runtime:**
  - Use whisper.cpp's own HTTP server (`whisper-server`). Its `/inference` endpoint takes
    multipart audio with `translate`, `language`, `response_format`, `temperature` and
    `prompt`; check the exact flags against the version you pin.
  - The adapter starts it the way LoadCoach starts other engines, then translates the OpenAI
    `/v1/audio/transcriptions` and `/v1/audio/translations` requests to `/inference`.
  - Map `response_format` (`json`, `text`, `srt`, `vtt`, `verbose_json`) onto whisper.cpp's
    output. Where `verbose_json` segments/words can't be matched, refuse that format with a
    clear 400 instead of approximating.
- **Install and update:**
  - [install.sh](../install.sh) and [scripts/install-suite.sh](../scripts/install-suite.sh): add
    `--whispercpp install|skip|keep` and `--whispercpp-backend cuda|vulkan|cpu`, mirroring
    `--audiocpp`.
  - Add an installer module like [soundroom/install.py](../soundroom/install.py) that builds a
    **pinned** whisper.cpp release from source with the chosen backend. Record the version in the
    install receipt ([scripts/lib/receipt.sh](../scripts/lib/receipt.sh)).
  - Cover it in [scripts/discard-install.py](../scripts/discard-install.py).
  - Make sure `owg update` keeps an installed whisper.cpp current, through the existing update
    implementation; follow the deployment rules in AGENTS.md.
  - Document it in [docs/installation.md](installation.md), beside audio.cpp.
- **Components:** the System → Components inventory ([loadcoach/components.py](../loadcoach/components.py))
  and the Telemetry workspace's installed-engines pills must list whisper.cpp when its program is
  found.
- **Engine sections:**
  - Node inference runtimes ([engine_nodes.html](../weightroom/pages/engine_nodes.html)).
  - The Audio model studio and model setup ([soundroom_models.html](../soundroom/pages/soundroom_models.html),
    [audio_model_parts.html](../weightroom/pages/audio_model_parts.html)), so a Whisper model can
    be added and configured per node.
  - The Models catalogue and model pages. Search for every `audiocpp` use in
    `weightroom/model_catalogue.py`, `weightroom/fit.py`, `loadcoach/loadouts.py`,
    `loadcoach/media_setup.py`, `loadcoach/platform_services.py` and
    `turnstile/model_configuration.py`.
  - Dry fit and loadouts must account for its memory.
  - Reuse the existing templates.
- **Model files:** whisper.cpp uses its own `ggml-*.bin` model files. They are **not**
  interchangeable with audio.cpp or llama.cpp GGUFs. The model setup must check the file type and
  refuse a mismatched engine/file pair.
- **Transcribe page and voice:** Whisper models should appear in Chat → Transcribe and the voice
  STT choices wherever audio.cpp transcription models do. Check `soundroom/voice.py`,
  `voice/options.py` and `voice/service.py`.

**Verification:**
- Unit and page tests mirroring the audiocpp ones.
- An installer test in the existing Docker qualification setup (`tests/docker/`) using the CPU
  backend.
- A real transcription and translation with the owner's **large-v3** model inside that container.
- Do not install on a real node without asking the owner. When approved, install on jpk-main
  with the new installer option and check Components and the Transcribe page live.

## T1. Audio translations

**Depends on W1.**

**Build:**
- Add `POST /v1/audio/translations` (multipart, OpenAI fields `model`, `file`, `prompt`,
  `response_format`, `temperature`), returning English text.
  - Multipart conversion: add the path beside `/v1/audio/transcriptions` in the turnstile branch
    (`turnstile/app.py`, the `request.url.path in ('/v1/audio/transcriptions', ...)` check).
  - Route it through LoadCoach `forward` with the audio capability check.
- Only models with the `translates` mark accept it; others get 400
  `"This model cannot translate speech."`. Whisper models set it from W1.
  - Also check whether the installed audio.cpp runtime can translate with `canary_asr` (the native
    `/v1/audio/transcriptions/details` call in [soundroom/runtime.py](../soundroom/runtime.py)).
    Mark it `translates` only if that works and is verified; otherwise leave Canary out and say so
    in the docs.
- Refuse `language`, since output is always English.
- Add the path to the observability route lists wherever `/v1/audio/transcriptions` appears.
- Show a "Translate to English" choice on Chat → Transcribe for models that can translate,
  extending the existing controls.

**Tests:**
- Multipart in the OpenAI SDK form shape.
- Each `response_format`.
- A non-translating model is refused; `language` is refused.
- The Transcribe page shows the option only for translating models.
- A real large-v3 translation in the W1 test container.

## K1. Reranker models and `/v1/rerank`

**Build:**
- **Model setting:** add a llama.cpp flag `reranking` (llama-server `--reranking`), handled the
  same way `embedding` is ([engines/llama_cpp/form.py](../engines/llama_cpp/form.py),
  [engines/llama_cpp/model.py](../engines/llama_cpp/model.py),
  [engines/llama_cpp/adapter.py](../engines/llama_cpp/adapter.py) capabilities:
  `rerank: true`, `chat`/`streaming`/`raw_completion: false`).
  - `embedding` and `reranking` are mutually exclusive.
  - Check the installed llama-server build supports `--reranking` and `/v1/rerank`
    (`llama-server --help`). If it doesn't, stop and report.
- **Mark:** `/v1/models` entries get `"rerank": true`, next to the `embedding` mark in
  [loadcoach/app.py](../loadcoach/app.py) `described`. Update the docstrings that explain the
  embedding mark ([loadcoach/models.py](../loadcoach/models.py)).
- **UI:** add "Reranker" wherever model types are chosen and filtered.
  - Search for `model_type_choices` (`navigation_parts.html`, Platform services) and for the
    Embedding model checkbox on the Models add form and model page.
  - Extend the existing controls and macros; don't copy them.
  - Keep Chat, Collections and the benchmarks from offering rerankers as chat or embedding models.
- **Endpoint:** `POST /v1/rerank` (also accept `/rerank`, rewritten to `/v1/rerank`) with
  Jina/Cohere fields `model`, `query`, `documents` (strings, 1–1,000), `top_n`,
  `return_documents`.
  - Validate it in LoadCoach `forward`, beside the audio/image capability checks: a model without
    `rerank` gets 400 `"Choose a reranker model for rerank requests."`.
  - Forward it to llama-server unchanged.
  - Add the path to the observability lists that name `/v1/embeddings`
    ([loadcoach/observability.py](../loadcoach/observability.py)), to usage accounting the same
    way as embeddings (no output tokens, see `loadcoach/waiting.py`), and to batch `ENDPOINTS`.
- **Install the two models on jpk-main** (approved). Use the existing Models → Downloads (Hugging
  Face) page or its install job, not manual copying:
  - **bge-reranker-v2-m3** (GGUF, Q8_0 or F16);
  - **Qwen3-Reranker-4B** (GGUF, Q8_0 or Q6_K). Qwen3 rerankers need a GGUF converted with
    llama.cpp's reranker support. Many community conversions return meaningless scores, so pick a
    repository whose card states llama.cpp `--reranking` support.

  Then mark each as Reranker.
- **Live check:** for both, a `/v1/rerank` request with one relevant and several irrelevant
  documents must rank the relevant one first with a clearly higher score. Record the request IDs
  and scores in the docs. If Qwen3 fails this, report it, and don't hide it by tuning the test.
- **Out of scope:** using rerank inside Collections. That's a separate owner decision.

**Tests:** form round trip and the two flags being mutually exclusive; the `/v1/models` mark;
forwarding through a fake engine; a non-reranker is refused; a reranker is refused for chat and
embeddings; a batch line; the UI audit.

## Order and reporting

Do them in order: **R1 → R3 → R4 → W1 → T1 → K1**. K1 doesn't depend on the others and may move
earlier while waiting on the owner (for example, for the large-v3 path or W1 install approval).

After each milestone, commit, push, then report:
- what changed (with file links) and the commit;
- the tests run and their results, with pre-existing failures listed separately;
- any decision you made where this brief left a choice;
- anything you stopped on.

The owner decides when to deploy.
