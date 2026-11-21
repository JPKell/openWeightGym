<!--docs
title = "Build an app on the API"
summary = "One self-contained brief for a developer or AI coding agent: addresses, authentication, every client endpoint, request fields, responses, errors and headers."
order = 3
covers = []
-->
# Build an app on the API

This page is written to be handed, whole, to a developer or an AI coding agent building a client
application. It states the client contract of the endpoints an application calls. Operator and
inter-service routes are left out; the [generated endpoint inventory](/docs/api/custom) lists
every declared route.

## Rules for the builder {#app-rules}

- The model API is **OpenAI-compatible**. Use an OpenAI SDK or plain HTTP with the base URL
  below. Where this page says "OpenAI-shaped", the OpenAI request and response formats apply,
  narrowed by the fields listed here.
- **Never hard-code model names.** Call `GET /v1/models` at start-up and offer only what it
  returns. Each account sees only the models and roles it may use. A role name (for example
  `docs-help`) can be sent wherever a model name goes.
- Pick a model by its `capabilities` before calling an endpoint: chat models for chat and
  Responses, `embedding: true` models for embeddings, `rerank: true` models for reranking, and
  image, video or audio models for those endpoints. A model refuses work of another kind with 400.
- Unknown request fields are **refused, not ignored**, on Responses and on the image, video,
  audio, rerank, classifier, extraction, file and batch endpoints. Send only the fields listed for
  that endpoint. Chat, completions and embeddings are passed to the model's engine instead (see
  [Chat](#app-chat)).
- Every request field, query parameter and header an app may send is listed on this page. A name
  that is not listed here is not part of the client contract.
- Images, audio and video are sent **inline** (base64 or data URLs) or as multipart uploads.
  Remote URLs and server file paths are refused.
- Treat every non-2xx response as an error object, as described in [Errors](#app-errors). Do not
  blindly retry writes or paid work: check the record first.

## Addresses and authentication {#app-connect}

| Listener | Default address | Used for |
|---|---|---|
| Model API | `http://CONTROL_NODE:8442/v1` | Every `/v1/…` endpoint and the `/owg/…` application endpoints below |
| Tools (MCP) | `http://CONTROL_NODE:8443/mcp` | MCP clients; `/mcp/docs` for Documentation tools only |
| Web console | `http://CONTROL_NODE:8440` | People in a browser; `/owg/docs/…` documentation reads |

`CONTROL_NODE` is the control node's host name or address, printed by the installer and by
`owg status`. Compute nodes' own ports are private: always call the control node.

Send `Authorization: Bearer API_KEY` on every request. A key is created under
**My account → API keys** and carries its account's model access, abilities, spending limits and
sensitivity ceiling. Keep it in the app's configuration or secret store, never in source or logs.
A missing or invalid key returns 401 `Missing or wrong API key.` A client that cannot set
headers may put the key in the address as `?key=API_KEY` instead; prefer the header, because
addresses end up in logs and browser history.

Some features need an ability on the account: tool calls need `use-tools`, unlogged requests need
`unlogged`, and high priority needs `high`. The [abilities reference](/docs/system/abilities)
explains each one.

To send work to one named compute node, add `?node=NAME` to the URL, the `X-Suite-Node: NAME`
header, or a `"node"` field where an endpoint's fields list it. This works only where the
administrator allows callers to choose a destination; leave it out to let the server choose.

## Models {#app-models}

`GET /v1/models` returns the account's models and roles as an OpenAI list. Each entry adds
openWeightGym facts:

```json
{"object": "list",
 "data": [{"id": "qwen3-8b", "object": "model", "owned_by": "loadcoach",
           "capabilities": {"chat": true, "streaming": true, "embeddings": false, "images": false,
                            "json_schema": true},
           "context": {"guaranteed": 8192, "max": 32768},
           "machines": ["gpu-1", "gpu-2"],
           "locations": [{"machine": "gpu-1", "enabled": true, "context": {"guaranteed": 8192, "max": 32768},
                          "loaded_context": 20480},
                         {"machine": "gpu-2", "enabled": true, "context": {"guaranteed": 8192, "max": 32768}}]},
          {"id": "nomic-embed", "object": "model", "owned_by": "loadcoach",
           "capabilities": {"chat": false, "embeddings": true},
           "embedding": true, "context_tokens": 8192, "context": {"guaranteed": 8192, "max": 8192},
           "machines": ["gpu-1"], "locations": [{"machine": "gpu-1", "enabled": true}]}],
 "available_machines": ["gpu-1"], "unavailable_machines": []}
```

- `capabilities` says what the model can do. Treat a missing key as "no", except `tools`, which
  is listed only when a model cannot call tools.
- `embedding: true` marks an embedding model and `rerank: true` a reranker. Like every other
  top-level mark, these keys are absent rather than false on other models. These models serve
  only their own endpoint.
- `context` is the model's per-request window, in tokens: a request within `guaranteed` always
  fits, and none can exceed `max`. The server loads the largest window that has room, so the
  window in use lies between the two. Prompt and answer (`max_tokens`) share it.
- `loaded_context`, in a location, is the window of the copy running on that node now. It is
  absent where the model is not loaded or the node did not answer. It is a snapshot: the model
  may be reloaded with another window before the next request. See
  [Context windows](#app-context).
- `context_tokens` appears on embedding models: the most tokens one input may have, equal to
  `context.guaranteed`.
- `unavailable_machines` lists nodes that did not answer. Their models may still be listed and
  can be requested; the server may wake an asleep node, which takes time.
- `GET /v1/models/NAME` returns one entry, or 404 when the account may not use that name.

## Endpoint index {#app-endpoints}

All paths are on the model API listener unless the table says otherwise. `{id}` is the resource
identifier returned when it was created.

| Endpoint | Purpose |
|---|---|
| `GET /v1/models` | Models and roles this account may use |
| `GET /v1/models/{id}` | One model entry |
| `POST /v1/chat/completions` | OpenAI chat, streaming, tools, JSON schema output, vision |
| `POST /v1/completions` | OpenAI raw text completion, for models whose `raw_completion` capability is true |
| `POST /v1/responses` | OpenAI Responses, streaming, server-run MCP tools, stored chains |
| `GET /v1/responses/{id}` | One stored response |
| `DELETE /v1/responses/{id}` | Delete a stored response |
| `GET /v1/responses/{id}/input_items` | Page through a stored response's inputs |
| `POST /v1/embeddings` | OpenAI embeddings, from an embedding model |
| `POST /v1/rerank` | Rank documents against a query, from a reranker (`/rerank` is an alias) |
| `POST /v1/classifier` | Typed classifier questions with probabilities |
| `POST /v1/systemone` | Native classifier questions, the same contract as `/v1/classifier` |
| `POST /v1/images/generations` | Generate images, or run a published image workflow |
| `POST /v1/images/edits` | Edit or extend reference images, with an optional mask |
| `GET /v1/workflows` | Published image workflows this account may run |
| `POST /v1/videos` | Queue one video clip |
| `GET /v1/videos` | List this account's video jobs |
| `GET /v1/videos/{id}` | One video job's status |
| `DELETE /v1/videos/{id}` | Delete a finished clip and its job |
| `POST /v1/videos/{id}/cancel` | Cancel a queued or running video job |
| `GET /v1/videos/{id}/content` | Download the WebM, or its poster with `?variant=thumbnail` |
| `POST /v1/videos/{id}/poster` | Attach a poster image when the server cannot make one |
| `POST /v1/videos/generations` | Internal render transport used by queued jobs; clients use `POST /v1/videos` |
| `POST /v1/audio/speech` | Speech from text, with built-in, cloned or designed voices |
| `POST /v1/audio/generations` | Music, sound or a multi-voice spoken script |
| `POST /v1/audio/transcriptions` | Speech to text |
| `POST /v1/audio/translations` | Speech to English text, for models that translate |
| `POST /v1/audio/diarizations` | Speech to text with speakers |
| `POST /v1/files` | Upload a batch input file |
| `GET /v1/files` | List this account's uploads |
| `GET /v1/files/{id}` | One upload's details |
| `DELETE /v1/files/{id}` | Delete an upload no batch is using |
| `GET /v1/files/{id}/content` | Download an upload or a batch's output |
| `POST /v1/batches` | Start a durable batch from an uploaded file |
| `GET /v1/batches` | List this account's batches |
| `GET /v1/batches/{id}` | One batch's progress and output file IDs |
| `POST /v1/batches/{id}/cancel` | Stop a batch; finished lines are kept |
| `GET /owg/extraction-models` | Models that can extract structured data from images |
| `POST /owg/extraction-assets` | Upload one source image for extraction |
| `GET /owg/extraction-assets/{id}` | Download an uploaded source |
| `DELETE /owg/extraction-assets/{id}` | Delete an uploaded source |
| `GET /owg/extraction-presets` | This account's saved extraction presets |
| `POST /owg/extraction-presets` | Save an extraction preset |
| `DELETE /owg/extraction-presets/{id}` | Delete a preset |
| `POST /owg/extractions` | Submit an image extraction job |
| `GET /owg/extractions` | Page through this account's extractions |
| `GET /owg/extractions/{id}` | One extraction and its result |
| `DELETE /owg/extractions/{id}` | Delete an extraction |
| `POST /owg/extractions/{id}/cancel` | Cancel pending extraction work |
| `GET /owg/extractions/{id}/export` | Download a result revision |
| `PATCH /owg/extractions/{id}/review` | Save corrections to a result |
| `GET /owg/docs/search` | Search the documentation this account may read (console listener) |
| `GET /owg/docs/article` | Read one documentation article (console listener) |
| `GET /owg/docs/catalogue` | List documentation topics (console listener) |

## Chat, completions and Responses {#app-chat}

`POST /v1/chat/completions` and `POST /v1/completions` are OpenAI-shaped and passed to the
model's engine, so the usual OpenAI and llama.cpp sampling fields apply (`temperature`, `top_p`,
`max_tokens`, `stop`, `seed` and so on). An engine may ignore a sampling field it lacks.
`POST /v1/embeddings` is passed on the same way. The server uses `model`, `stream` and the token
budget to route and admit the request, and checks tools, images and structured output against the
model's capabilities (below). The body otherwise reaches llama.cpp's
[server API](https://github.com/ggml-org/llama.cpp/tree/master/tools/server#api-endpoints)
unchanged, except `lora`, which the server replaces with the adapters the model is configured
with.

Two kinds of chat model accept less:

**Strata chat fields:** `model`, `messages`, `max_tokens`, `max_completion_tokens`, `stream`, `stream_options`, `temperature`, `top_p`, `top_k`, `min_p`, `seed`, `stop`, `reasoning_effort`

A model on the experimental Strata engine refuses any other field, and takes only `system`,
`user` and `assistant` messages with text content.

**Agent chat fields:** `model`, `messages`, `stream`, `reasoning_effort`, `n`

A model whose `owned_by` is `adaptor` is an agent run by a plugin. It runs its own tools, so it
refuses `tools`, `tool_choice`, `functions` and `function_call`; `n` must be 1. Any other field is
dropped and named in the `X-Suite-Ignored-Parameters` response header. Its session headers are
under [Headers](#app-headers).

```json
{"model": "qwen3-8b", "messages": [{"role": "user", "content": "Say hello."}], "max_tokens": 64}
```

- Read the answer from `choices[0].message.content` and the counts from `usage`.
- `"stream": true` returns server-sent events: `data: {chunk}` lines with
  `choices[0].delta.content`, a last chunk with `finish_reason` and `usage`, then `data: [DONE]`.
- **Tools:** send OpenAI `tools`; the app runs each function and sends back a `tool` message. It
  needs a model whose `capabilities.tools` is not false and the `use-tools` ability; otherwise 400
  `unsupported_tools` or 403 `tools_not_allowed`.
- **Structured output:** `response_format: {"type": "json_schema", "json_schema": {...}}` on models
  whose `capabilities.json_schema` is true.
- **Vision:** `image_url` content parts with `data:image/...;base64,` URLs, on models whose
  `capabilities.images` is true.

`POST /v1/responses` is OpenAI-shaped. It accepts text input, inline image data, sampling
controls and text, JSON or schema formatting.

```json
{"model": "qwen3-8b", "input": "Say hello.", "store": false}
```

- A result has `object: "response"`, a `status`, and `output` items. The text is in the
  `message` item's `output_text` content.
- `store: false` keeps no record. Otherwise the response is stored for its owner, and
  `previous_response_id` continues it. New `instructions` replace earlier ones.
- `"stream": true` returns Responses events. Treat `response.completed`, `response.failed` and
  `response.incomplete` as the end.
- Server-run tools: declare `{"type": "mcp", "server_label": "toollocker", "server_url": "/mcp/docs"}`
  with optional `allowed_tools`. Other MCP servers are refused. Function tools run in the app.
- Refused: `background`, remote image or file URLs, automatic truncation and other OpenAI
  resource parameters.

**Responses fields:** `model`, `input`, `instructions`, `text`, `store`, `stream`, `max_output_tokens`, `temperature`, `top_p`, `previous_response_id`, `background`, `truncation`, `tools`, `tool_choice`, `parallel_tool_calls`

`model` is required. `input` is a string or 1–100 input items. `max_output_tokens` is 1–32768,
`temperature` 0–2 and `top_p` 0–1. `background` may only be `false` and `truncation` only
`"disabled"`. `previous_response_id` names a stored response of this account.

**Responses message item fields:** `type`, `role`, `content`, `id`, `status`

`type` is `message` (the default); `role` is `user`, `assistant`, `system` or `developer`.
`content` is a string or a list of parts: `{"type": "input_text", "text": ...}` (or
`output_text`), or an image part:

**Responses image part fields:** `type`, `image_url`, `detail`

`type` is `input_image`; `image_url` is a `data:image/png`, `jpeg` or `webp` base64 URL;
`detail` is `auto` (default), `low` or `high`.

**Responses function_call item fields:** `type`, `id`, `status`, `call_id`, `name`, `arguments`

**Responses function_call_output item fields:** `type`, `id`, `call_id`, `output`

**Responses mcp_approval_response item fields:** `type`, `id`, `approval_request_id`, `approve`, `reason`

A `function_call` item replays a call the model made (`call_id` 1–128 characters, `arguments` a
JSON string); `function_call_output` returns the app's result as a string. An
`mcp_approval_response` answers an approval request of a server-run tool.

**Responses text format fields:** `type`, `name`, `schema`, `strict`, `description`

`text` is `{"format": {...}}`: `{"type": "text"}`, `{"type": "json_object"}`, or `json_schema`
with a `name` (1–64 characters), a `schema` and an optional boolean `strict`.

**Responses function tool fields:** `type`, `name`, `description`, `parameters`, `strict`

**Responses MCP tool fields:** `type`, `server_label`, `server_url`, `allowed_tools`, `require_approval`

A function tool has `type: "function"`; `tool_choice` is `auto`, `none`, `required` or
`{"type": "function", "name": ...}`. An MCP tool has `type: "mcp"` and `server_label: "toollocker"`;
`server_url` is `/mcp` (the default) or `/mcp/SET` on this server. `allowed_tools` lists at most 128
tool names. `require_approval` is `never` (default), `always`, or `{"never": {"tool_names": [...]}}`
/ `{"always": {"tool_names": [...]}}`. Server-run tools need a logged request, and approvals need
`store: true`.

**JSON Schema keywords:** `type`, `properties`, `required`, `additionalProperties`, `items`, `enum`, `description`, `title`, `minimum`, `maximum`, `minLength`, `maxLength`, `minItems`, `maxItems`, `pattern`

These are the keywords allowed in a Responses `json_schema`, a Responses function tool's
`parameters` and a custom extraction `schema`. Chat completions pass their schema to the engine.

OpenAI Assistants, Conversations, Realtime and fine-tuning are not implemented.

## Context windows {#app-context}

A chat or completion request is served with a window between the model's `context.guaranteed`
and `context.max`. Every answer says which in its `X-LoadCoach-Context-Tokens` header.

- A request that needs more than the loaded window, but not more than `max`, makes the server
  reload the idle model with a larger window when there is room now. The server counts the
  prompt roughly, about three bytes per token, plus `max_tokens`. The reload adds a load's
  delay to that request; the model then stays at the larger window.
- When there is no room, the server does not wait or stop anyone else's work. The engine judges
  the prompt against the loaded window, and a prompt that does not fit is refused at once with
  400 `context_too_small`. The error carries `loaded_context` and `max_context`:

```json
{"error": {"code": "context_too_small", "type": "exceed_context_size_error", "loaded_context": 20480, "max_context": 32768,
           "message": "the request exceeds the available context size, try increasing it. qwen3-8b is loaded with a 20480-token window now and allows up to 32768; shorten the request, or retry when there is room to load it larger."}}
```

When `loaded_context` is less than `max_context`, the app may shorten the request or retry
later. When they are equal, only a shorter request fits. Keep prompts within `guaranteed` when an
answer must not depend on what else is loaded.

## Thinking (reasoning) {#app-thinking}

Some models think before they answer. Whether they do is set per model by the administrator, and
a chat or completion request may change it for itself.

**Model default.** A model's settings may include llama-server's `reasoning` (`on`, `off` or
`auto`), `reasoning-effort`, `reasoning-budget` (0 turns thinking off), `reasoning-format` and
`chat-template-kwargs`. Requests that say nothing use them.

**Per request, on `POST /v1/chat/completions` and `/v1/completions`.** Both fields go to the
engine unchanged:

- `"chat_template_kwargs": {"enable_thinking": false}` turns thinking off (or `true` on) for
  models whose chat template has that switch, such as Qwen3. A template without it ignores the
  field. The Strata engine accepts only `{"enable_thinking": false}`.
- `"reasoning_effort": "low"` chooses how hard the model thinks, for templates that support it.
  `"none"` asks for no thinking.

```json
{"model": "qwen3-8b", "messages": [{"role": "user", "content": "Say hello."}],
 "chat_template_kwargs": {"enable_thinking": false}}
```

**Discover support** in `GET /v1/models`. While a copy is loaded on a node, that node's location
carries `chat_template_caps`, as the running engine reports them. Examples are
`supports_reasoning_effort` and `reasoning_effort_levels` (lowest to highest). Where the model is
not loaded, or the engine is older, the field is absent. Nothing in the
API says whether a template has an `enable_thinking` switch; sending it is harmless when not.

**Read the thinking.** When the model's reasoning format separates it, the thinking is
`choices[0].message.reasoning_content`, and `choices[0].delta.reasoning_content` in a stream.
Otherwise it stays inside `content`, often between `<think>` tags. Responses returns it as a
`reasoning` output item whose `summary` holds the text, and counts it in
`usage.output_tokens_details.reasoning_tokens`. Responses does not accept a `reasoning`
parameter (400 `unsupported_parameter`): a Responses request thinks as the model's settings say.

Thinking tokens count against `max_tokens` and the [context window](#app-context). A
low `max_tokens` can end an answer during its thinking, leaving `content` empty.

## Embeddings, reranking and classifiers {#app-vectors}

`POST /v1/embeddings` is OpenAI-shaped. Choose a model whose `embedding` is true. `input` is a
string or a list of strings, each within the model's `context_tokens`; the vectors are in
`data[i].embedding`, in input order.

```json
{"model": "nomic-embed", "input": "How do I reset a key?", "encoding_format": "float"}
```

`POST /v1/rerank` takes a query and up to 1000 text documents, in all at most 32 MiB of text.
`top_n` (1–1000) limits the results, and `return_documents` copies the text back. The response
lists `results` with each document's `index` and `relevance_score`; sort by the score rather than
relying on the order.

**Rerank fields:** `model`, `query`, `documents`, `top_n`, `return_documents`

```json
{"model": "bge-reranker", "query": "How do I reset a key?", "documents": ["Keys are made in My account.", "Models are listed in /v1/models."], "top_n": 1}
```

`POST /v1/classifier` asks a classifier model typed questions about some content and returns a
probability for each answer. Probabilities are uncalibrated. Send `state` (text, an object or an
array) or chat `messages`, not both. `questions` holds 1–64 questions keyed by an ID of your
choice. Each question has a `type`, `instructions` and, depending on the type, `criteria`:

- `choice`: an object of 2–255 option labels, each with a description or `null`;
- `score`: a list of 2–10 ordered level descriptions;
- `noul`: an optional `{"true": ..., "false": ...}` description, for a yes/no answer.

`images` and `files` each take up to 16 inline image data URLs. The whole body must fit in 64 KiB.

**Classifier fields:** `model`, `state`, `messages`, `questions`, `files`, `images`, `node`

**Classifier question fields:** `type`, `instructions`, `criteria`

**Classifier noul criteria fields:** `true`, `false`

```json
{"model": "classifier-small", "state": "The order arrived broken and support never replied.",
 "questions": {"sentiment": {"type": "choice", "instructions": "Overall sentiment",
                             "criteria": {"positive": null, "neutral": null, "negative": null}},
               "urgent": {"type": "noul", "instructions": "Does this need a reply today?"}}}
```

A chat completion to a classifier model with a `response_format.json_schema` whose properties are
enums, booleans or bounded integers is answered the same way. Such a request takes only these
fields, and its schema and properties only these keys:

**Classifier chat fields:** `model`, `messages`, `response_format`, `stream`, `logprobs`, `top_logprobs`, `node`

**Classifier schema fields:** `type`, `properties`, `required`, `additionalProperties`, `title`, `description`

**Classifier property fields:** `type`, `enum`, `description`, `minimum`, `maximum`, `title`, `x-levels`

`x-levels` is an extension on an integer property: a list of 2–10 level descriptions, scored from
`minimum` (default 0) upward.

## Images and image workflows {#app-images}

`POST /v1/images/generations` and `POST /v1/images/edits` accept JSON, or the OpenAI SDK's
multipart form (`image`/`image[]` files and an optional `mask`). A request may be up to 60 MiB and
each image up to 20 MiB, as PNG, JPEG or WebP, base64 or a data URL.

**Image fields:** `model`, `prompt`, `negative_prompt`, `n`, `size`, `width`, `height`, `steps`, `seed`, `guidance`, `cfg_scale`, `sampler`, `scheduler`, `references`, `image`, `mask`, `strength`, `loras`, `upscale`, `stream`, `partial_images`, `response_format`, `output_format`, `user`, `quality`, `background`, `moderation`, `metadata`, `live_previews`, `reference_megapixels`, `detail_resize`, `advanced`, `upscale_with`, `control`

```json
{"model": "sdxl-base", "prompt": "A lighthouse at dusk, oil painting", "size": "1024x1024", "n": 1, "seed": 7}
```

- `prompt` is 1–24000 characters. Give `size` as `WIDTHxHEIGHT` or `width`/`height`; each side is
  at least 256, a multiple of 16 (32 for Qwen Image), within the model's limits.
- `seed` -1 picks one at random. `strength` (0–1) sets how far an edit departs from its image.
- Results are PNG as `b64_json` only: `{"created": ..., "data": [{"b64_json": "..."}]}`. Other
  `response_format` or `output_format` values are refused. Images are also saved to the
  account's library.
- `"stream": true` returns events; each finished image arrives as an
  `image_generation.completed` event with `b64_json`.
- `references` (or `image`, not both) is a list of input images and `mask` one image; `loras` is
  `{"ADAPTER": scale}` for at most 16 of the model's configured adapters; `upscale` is a factor
  of 1–4; `detail_resize` is `{"width": W, "height": H}`, the exact final size of an SDXL
  resize-and-detail pass; `metadata` is the caller's own record.
- `advanced`, `control` and `upscale_with` take these keys. `advanced` needs an exclusive model
  entry, and its `slg` and `hires` values are objects of their own. The
  [Image generation guide](/docs/web-app/image-generation) describes each and the families it
  applies to.

**Image advanced fields:** `clip_skip`, `eta`, `img_cfg`, `flow_shift`, `shifted_timestep`, `custom_sigmas`, `slg`, `hires`, `vae_tiling`, `cache_mode`, `cache_option`

**Image slg fields:** `scale`, `layer_start`, `layer_end`, `layers`

**Image hires fields:** `upscaler`, `scale`, `steps`, `denoising_strength`

**Image control fields:** `image`, `strength`, `edges`

**Image upscale_with fields:** `upscaler`, `factor`, `tile`

**Workflows:** `GET /v1/workflows` lists published multi-step workflows with their `id`
(`workflow:NAME`), inputs, overridable fields and bounds, outputs and models. Run one by sending
its `id` as the `model` of an image request, with `workflow_inputs` for exposed inputs:

```json
{"model": "workflow:sdxl-two-pass-hires", "prompt": "A lighthouse at dusk", "seed": 7, "n": 2,
 "workflow_inputs": {"base.steps": 30}}
```

**Workflow call fields:** `model`, `prompt`, `negative_prompt`, `size`, `seed`, `n`, `image`, `workflow_inputs`, `stream`, `user`, `response_format`, `output_format`, `quality`, `background`, `moderation`

A workflow call takes only these fields; its other settings come from the workflow.
`workflow:NAME@VERSION` runs one published version. Each output in `data` has its `output`
label, `step`, `seed`, size and `asset_id`. The
[Image workflows guide](/docs/model-guides/image-workflows#calling-a-workflow-from-the-api)
describes the inputs and stream events.

## Video {#app-video}

Video is asynchronous: create a job, poll it, then download the clip.

**Video fields:** `model`, `prompt`, `negative_prompt`, `size`, `width`, `height`, `seconds`, `fps`, `frames`, `seed`, `steps`, `guidance`, `sampler`, `scheduler`, `flow_shift`, `high_noise_steps`, `high_noise_guidance`, `moe_boundary`, `input_reference`, `init_image`, `end_image`, `reference_images`, `control_video`, `vace_strength`, `loras`, `soundtrack`, `vae_tiling`, `temporal_tiling`, `hires`, `output_format`, `metadata`, `stream`, `live_previews`

```json
{"model": "wan21-small", "prompt": "A red sailboat moving across a calm lake.", "seconds": 2.1, "size": "832x480", "seed": 42}
```

1. `POST /v1/videos` returns a job with an `id` and a `status` of `queued`.
2. Poll `GET /v1/videos/{id}` every few seconds. `status` moves through `in_progress` to
   `completed` or `failed`; a failed job's `error.code` says whether it failed, was cancelled or
   expired.
3. Download `GET /v1/videos/{id}/content` (WebM, with byte ranges), or
   `?variant=thumbnail` for its poster.

Dimensions, frame rate, frames and seconds are bounded by the model's profile. Input images are
inline or owned asset IDs. `loras` is `{"ADAPTER": strength}` for at most 16 configured adapters;
`soundtrack`, `vae_tiling`, `temporal_tiling`, `hires`, `stream` and `live_previews` are booleans.

**Video poster fields:** `b64_json`

`POST /v1/videos/{id}/poster` takes exactly that: a base64 image to use as a finished clip's
poster when the server could not make one. The [Video generation guide](/docs/web-app/video-generation) lists
the differences from the retired OpenAI Videos API.

## Audio {#app-audio}

`POST /v1/audio/speech` is OpenAI-shaped and returns the audio file itself. `response_format` is
`wav`, `mp3` (default), `flac`, `opus` or `pcm`. `voice` names a voice the model offers. A cloning
model also needs `voice_audio` (base64 recording) and `voice_text` (its transcript); a
voice-design model needs `instructions` describing the voice.

**Speech fields:** `model`, `input`, `voice`, `voice_audio`, `voice_text`, `instructions`, `response_format`, `speed`, `seed`, `options`, `stream_format`, `user`

```json
{"model": "kokoro", "input": "Your order has shipped.", "voice": "af_heart", "response_format": "mp3"}
```

`POST /v1/audio/transcriptions`, `/v1/audio/translations` and `/v1/audio/diarizations` accept
the OpenAI SDK's multipart form (a `file` field), or JSON with `file` as base64 audio. A request
may be up to 96 MiB. `response_format` is `json` (default), `text`, `srt`, `vtt` or
`verbose_json`. Translation always returns English, so it takes no `language`.

**Transcription fields:** `model`, `file`, `language`, `prompt`, `response_format`, `timestamp_granularities`, `temperature`, `user`

`POST /v1/audio/generations` makes music or sounds from a `prompt`, optionally with `lyrics`, or a
spoken `script` of up to 500 lines, each with its own voice.

**Music and sound fields:** `model`, `prompt`, `lyrics`, `instrumental`, `duration`, `seed`, `n`, `mode`, `audio`, `repaint_start`, `repaint_end`, `options`, `loop`, `stream`, `user`

**Script fields:** `model`, `script`, `seed`, `gap`, `options`, `stream`, `user`

**Script line fields:** `text`, `voice`, `voice_audio`, `voice_text`, `instructions`

Each script line needs `text`; the others are optional. Which modes and options a model takes depends on its family; an unsupported
combination returns 400 naming what the model accepts. The [Music and sounds](/docs/web-app/music-sounds)
and [Speech](/docs/web-app/speech) guides describe the families.

## Files and batches {#app-batches}

Batches run many requests durably, in the OpenAI Batch format.

1. Write a JSONL file, one request per line:
   `{"custom_id": "a1", "method": "POST", "url": "/v1/chat/completions", "body": {...}}`.
2. Upload it with `POST /v1/files` as a multipart form, with `purpose` set to `batch` and the
   JSONL file as `file`:

**File upload form fields:** `purpose`, `file`

3. `POST /v1/batches` with the returned file `id`:

**Batch fields:** `input_file_id`, `endpoint`, `completion_window`, `metadata`

```json
{"input_file_id": "file-123", "endpoint": "/v1/chat/completions", "completion_window": "24h"}
```

4. Poll `GET /v1/batches/{id}` for `status` and `request_counts`, then download
   `output_file_id` and `error_file_id` with `GET /v1/files/{id}/content`.

**Batch endpoints:** `/v1/chat/completions`, `/v1/responses`, `/v1/completions`, `/v1/embeddings`, `/v1/rerank`, `/v1/images/generations`, `/v1/images/edits`, `/v1/audio/generations`, `/v1/videos/generations`

`endpoint` and each line's `url` must be one of these; a video line takes the
[video fields](#app-video). Lines that do not suit the endpoint's models are refused before the batch
starts. File size and line limits are set by the administrator; a file over the limit returns 413.

## Image extraction {#app-extraction}

Extraction reads text, receipts, tables or custom fields from an image with a vision model, and
keeps the result for review. These endpoints are on the model API listener.

1. `GET /owg/extraction-models` lists the models this account may use for extraction.
2. Upload the image: `POST /owg/extraction-assets` as a multipart form with exactly one file
   part named `image` (or `file`). The response's `id` is the `asset_id`.
3. Submit a job with `POST /owg/extractions`:

**Extraction fields:** `asset_id`, `task`, `model`, `node`, `output_format`, `instructions`, `detail`, `schema`, `keep_breaks`, `mark_unclear`, `temporary`, `retry_of`

```json
{"asset_id": "ASSET_ID", "model": "qwen2.5-vl", "task": "receipt", "output_format": "json"}
```

4. Poll `GET /owg/extractions/{id}` until `status` is `completed` or `needs_review`, then read
   `text` (rendered output), `data` (structured result) and `warnings`.

`task` is `describe`, `text` (default), `receipt`, `table` or `custom`; `custom` needs a JSON
`schema`. `output_format` is `plain` (default), `layout`, `markdown`, `json` or `csv`; CSV is
for receipts and tables only. `detail` is `concise` or `detailed`. `schema` uses the
[JSON Schema keywords](#app-chat) listed under Responses. `temporary: true` keeps the job for a day
instead of 30 days; `retry_of` names an earlier extraction of this account to retry. Send an `Idempotency-Key` header (at most
120 printable characters, no spaces) so a resent submit returns the first job instead of a second.

**Extraction preset fields:** `name`, `spec`

`POST /owg/extraction-presets` saves `spec`, which takes the extraction fields above except
`asset_id`.

**Extraction review fields:** `revision`, `data`, `reviewed`

`PATCH /owg/extractions/{id}/review` saves corrected `data` against the job's current integer
`revision` (a stale revision returns 409), with `reviewed` true or false. The
[Image extraction guide](/docs/web-app/image-extraction) covers review and exports.

## Errors {#app-errors}

Errors are JSON objects in the OpenAI shape:

```json
{"error": {"message": "The model 'qwen3-9b' does not exist. Available: qwen3-8b", "type": "loadcoach_error", "code": "model_not_found"}}
```

Show `message` to the user. Branch on the status and `code`, not on the message text.

| Status | Meaning | What the app should do |
|---|---|---|
| 400 | Invalid input: an unknown field, a value out of range or a model of the wrong kind | Fix the request; do not retry it unchanged |
| 400 `context_too_small` | The prompt and answer exceed the loaded window | Shorten it, or retry later if `loaded_context` < `max_context` ([Context windows](#app-context)) |
| 401 | Missing, wrong, revoked or expired key | Ask for a new key |
| 403 | The account lacks the model, ability or sensitivity level | Explain; do not retry |
| 404 | No such model, or a record that is missing or not this account's | Refresh `GET /v1/models` or the list |
| 409 | A required model identity changed, or the request conflicts with current state | Read `message` before retrying |
| 413 | The body is too large | Send less |
| 429, 503 | Busy, out of allowance, or the model's node is unavailable | Wait for `Retry-After` when present; a spent allowance does not recover by retrying |
| 502, 504 | The model engine failed or timed out | Retry later, once |

A stream that started with 200 can still end early. Check for a final chunk or terminal event.

## Query parameters {#app-query}

Two parameters work on any endpoint: `key` (the API key, when the client cannot send
`Authorization`) and `node` (the compute node to use, where callers may choose). The endpoints
below read these as well; an endpoint not listed takes no other query parameters.

| Endpoint | Parameters | Values |
|---|---|---|
| `GET /v1/videos` | `after`, `limit`, `order` | `after` is the last job ID of the previous page; `limit` 1–100 (default 20); `order` `asc` or `desc` (default) |
| `GET /v1/videos/{id}/content` | `variant` | `video` (default, WebM) or `thumbnail` (the poster) |
| `GET /v1/responses/{id}/input_items` | `after`, `limit`, `order` | As for videos, with item IDs |
| `GET /owg/extractions` | `cursor` | The previous page's `next_cursor` |
| `GET /owg/extractions/{id}/export` | `format`, `revision` | `format` is an output format (default the job's); `revision=original` exports the result before corrections |
| `GET /owg/extraction-assets/{id}` | `prepared` | `1` downloads the PNG the model was sent instead of the original upload |
| `GET /owg/docs/catalogue` | `offset` | Paging offset, in steps of 20 (`next_offset`) |
| `GET /owg/docs/search` | `q`, `scope`, `limit` | Query text; a section slug from the catalogue; 1–10 results |
| `GET /owg/docs/article` | `url`, `anchor`, `offset` | An article `url` from the catalogue or a search hit; an optional section `anchor`; a character `offset` for long articles |

A documentation request with an unknown or repeated parameter is refused with 400.

## Headers {#app-headers}

Header names are case-insensitive. Every header an app may send is in the first two tables; the
third lists what responses carry. The fourth names the server's own headers so they are not
mistaken for options.

**Standard headers an app sends**

| Header | Use |
|---|---|
| `Authorization: Bearer API_KEY` | Every request ([Addresses and authentication](#app-connect)) |
| `Content-Type` | `application/json`, or `multipart/form-data` for uploads and the OpenAI SDK's image and audio forms |
| `Idempotency-Key` | On `POST /owg/extractions` (at most 120 printable characters, no spaces) and on agent chat models (1–128 characters): a resend with the same key returns the first result; reusing it with a different body returns 409 |
| `Range` | A byte range of `GET /v1/videos/{id}/content`; 416 when it cannot be served |
| `traceparent` | Optional W3C trace context, adopted when valid |
| `User-Agent` | Shown beside the key's sessions under **My account → API keys** |
| `Mcp-Session-Id` | MCP clients on the tools listener; groups the session's calls when no trace is sent |

**openWeightGym headers an app may send**

| Header | Effect |
|---|---|
| `X-Suite-Application: my-app` | Labels the app's requests in usage and logs (letters, digits, `.` `_` `:` `-`, up to 128). Invalid labels are not recorded; it grants nothing |
| `X-Suite-Node: NAME` | The compute node to use, as `?node=NAME` does |
| `X-LoadCoach-Trace: job-42` | Groups related calls as one piece of work (1–128 letters, digits, `.` `-` `_` `:`). A trace keeps its highest sensitivity |
| `X-LoadCoach-Priority: high` | `high`, `normal` or `low` admission order; high needs the `high` ability |
| `X-LoadCoach-Sensitivity: sensitive` | `normal`, `sensitive` or `restricted`, within the account's allowance; non-normal work stays local |
| `X-LoadCoach-Log: off` | Keeps no record, where the account has `unlogged`. No other value; not with trace or sensitivity headers, hosted models or offload |
| `X-Turnstile-No-Wake: 1` | Fail fast with 503 instead of waking an asleep node |
| `X-LoadCoach-Offload: MODEL` | A hosted model the server may use instead when the local one would wait, subject to access, sensitivity and spending policy |
| `X-LoadCoach-Require-Model-ID`, `X-LoadCoach-Require-Fingerprint`, `X-LoadCoach-Require-Build` | Sent together (each 1–256 characters, from an earlier answer's `X-LoadCoach-Model-ID`, `X-LoadCoach-Configured-Fingerprint` and `X-LoadCoach-Build`), they pin the exact local model, settings and engine build; a mismatch returns 409 before inference |
| `X-LoadCoach-Require-Model-Name: NAME` | Requires this exact local model name on any node; hosted models and offload are refused |
| `X-LoadCoach-Lock: TOKEN` | Runs on GPUs the caller reserved with `POST /loadcoach/lock` (the `lock` ability; an operator tool, not needed by apps) |
| `X-Suite-Session-Mode` | Agent chat models only: `fresh` (default) or `persistent` |
| `X-Suite-Session-ID` | Agent chat models only: resume a persistent session returned earlier |
| `X-ToolLocker-Sets: a,b` | MCP clients: limit the tools listed to these comma-separated tool sets, within the account's permissions |

**Response headers an app may read**

| Header | Meaning |
|---|---|
| `X-Request-ID` | On every response. Log it with errors |
| `X-LoadCoach-Request-ID` | The request number administrators see under **Model requests**; absent for unlogged work |
| `X-LoadCoach-Context-Tokens` | The context window a chat or completion answer was served with ([Context windows](#app-context)) |
| `X-LoadCoach-Model-ID`, `X-LoadCoach-Configured-Fingerprint`, `X-LoadCoach-Fingerprint`, `X-LoadCoach-Build` | Identity of the local model that answered: its ID, the fingerprint of its settings as configured and as started, and the engine build. Send the first three back as the `Require` headers to pin them |
| `X-LoadCoach-Answered`, `X-LoadCoach-Answered-ID`, `X-LoadCoach-Answered-Fingerprint` | The local model that answered, after any role pick: its name, ID and settings fingerprint |
| `X-LoadCoach-Role`, `X-LoadCoach-Picked`, `X-LoadCoach-Because` | For a role: the role, the model it picked and why |
| `X-LoadCoach-Node`, `X-Turnstile-Node` | The node that answered, or the node that did not |
| `X-Turnstile` | `compute-off` or `busy` on a 503 that came from the gateway rather than the model |
| `X-LoadCoach-Outcome` | How compute ended a request it refused or could not load |
| `X-LoadCoach-Estimated-Wait`, `X-LoadCoach-Estimate-Basis` | Advisory queue wait in seconds (or `unknown`) and how it was estimated |
| `X-LoadCoach-Hosted`, `X-LoadCoach-Service` | A hosted model answered: its name and provider |
| `X-Suite-Allowance` | JSON list of spending allowances near their limit (`scope`, `name`, `recorded`, `allowance`, `remaining`, `reset_at`), when the administrator warns instead of blocking |
| `X-Suite-Ignored-Parameters` | Agent chat models: the comma-separated fields that were dropped |
| `X-Suite-Image-Temporary`, `X-Suite-Audio-Temporary` | `1` when the generated file is not kept in the library |
| `X-Suite-Retained` | `1` when a listing was answered from the last known state because a node did not answer |
| `X-Suite-Data-Owner` | The control node answered from its own records |
| `X-Suite-Proof` | Signature of a worker's answer, checked by the gateway; apps can ignore it |
| `Retry-After` | Seconds to wait, on 429 and 503 |
| `X-Content-Type-Options`, `X-Accel-Buffering` | Standard `nosniff` and stream buffering hints |

**Server headers an app must not send**

These pass between openWeightGym's own services. The gateway strips or ignores them from
clients, or they need a signature an app cannot make; sending one does nothing useful.
`X-Suite-Statement`, `X-Suite-Client-Origin`, `X-Suite-Schedule`, `X-Suite-User-ID`,
`X-Suite-Instance`, `X-Suite-Model`, `X-Suite-Profile`, `X-Suite-Request-ID`, `X-Suite-Adaptor`,
`X-Suite-Signature`, `X-Suite-Dump-Method`, `X-Suite-Image-Owner`, `X-Suite-Audio-Owner`,
`X-LoadCoach-For`, `X-LoadCoach-Role-Context`, `X-LoadCoach-From`, `X-LoadCoach-Agent`,
`X-LoadCoach-Proxy-Seen`, `X-LoadCoach-Task`, `X-LoadCoach-Probe-ID`, `X-LoadCoach-Image-Owner`,
`X-LoadCoach-Upscaler-Path`, `X-MirrorWall-Job`, `X-MirrorWall-Line`, `X-MirrorWall-Intermediate`,
`X-SoundRoom-Job`, `X-SoundRoom-Line`, `X-Hermes-Session-Key`, `X-Twilio-Signature`,
`X-Weightroom-Form-Return`, `X-Turnstile-Report-Cache` and `X-Forwarded-For`. Roles are chosen
with the `model` field, never a header.

[Headers](/docs/api/headers) explains each in more depth.

## Related

[Authentication and keys](/docs/api/authentication) · [Examples](/docs/api/examples) ·
[OpenAI compatibility](/docs/api/openai) · [MCP endpoint](/docs/api/mcp) ·
[Generated endpoint inventory](/docs/api/custom)
