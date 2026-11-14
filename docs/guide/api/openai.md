<!--docs
title = "OpenAI compatibility"
summary = "Choose a supported model API workflow and understand backend-dependent behavior."
order = 10
covers = []
-->
# OpenAI compatibility

Use OpenAI-compatible clients with configured openWeightGym models and roles.

## Implemented OpenAI endpoints {#api-compatible}

The [generated endpoint inventory](/docs/api/custom) lists declared routes, handler summaries and access checks from this checkout. It is the reference for paths and methods. Engine and model capabilities still determine which inputs succeed.

| Workflow | Choose and check |
| --- | --- |
| Text and vision | Use a chat-capable model; vision also needs a supported model/projector and backend. [Model configuration](/docs/model-guides/configuration) explains placement and capabilities. |
| Responses | Use the ordinary chat admission path with Responses input/output adaptation. See the options below and [Stored responses](/docs/monitoring/responses). |
| Embeddings and reranking | Choose a model configured for that mode; a reranker cannot serve chat or embeddings. |
| Images and audio | Enable the matching service and configure a compatible model. Follow the [Web app media guides](/docs/web-app). |
| Durable batch work | Upload owned batch input and submit supported work. See [Batches](/docs/monitoring/batches). |

## Responses options

Send `store:false` when the client needs no retained Responses record. Otherwise the existing Responses storage rules apply; retention defaults live in the [settings reference](/docs/operations/settings-reference). A continuation uses `previous_response_id` from the same owner and current clearance. New instructions replace earlier instructions.

`stream:true` returns Responses SSE events, including text deltas, tool activity, terminal status and usage. Treat `response.failed` and `response.incomplete` as terminal outcomes too.

Function tools execute in the client. Server-run ToolLocker tools use one MCP definition with `server_label: toollocker` and a caller-permitted local tool set. See [MCP endpoint](/docs/api/mcp). Tool requests require [use-tools](/docs/system/abilities#ability-use-tools), even when a cloud model or batch line handles them.

Responses accepts text, supported inline image data, sampling controls and text/JSON/schema formatting. Background execution, remote image/file references, automatic truncation and unrelated OpenAI resource parameters are rejected. A backend's native features do not automatically add them to this adapter.

## Not implemented by openWeightGym {#api-unsupported}

The broad worker inference proxy offers backend-dependent passthrough for additional model requests. This does not promise implementations of OpenAI Conversations, Realtime, Assistants, fine-tuning or other resource families. Hosted forwarding is likewise provider-dependent.

openWeightGym conversations, collections, request logs and account administration use their own application contracts. Match clients to the generated inventory and check a small request with the selected backend before a larger job.

## Related

[Authentication and keys](/docs/api/authentication), [Examples](/docs/api/examples), [Headers](/docs/api/headers).
