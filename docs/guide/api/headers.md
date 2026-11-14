<!--docs
title = "Headers"
summary = "The request headers that change how work is admitted, traced and logged, and the headers that responses carry."
order = 30
covers = []
-->
# Headers

## Request headers {#api-headers}

### Common

Header names are case-insensitive. Ordinary HTTP headers still apply: `Authorization`, appropriate `Content-Type` (usually `application/json`, or multipart where listed), and WebSocket handshake headers. An ignored header does not enable a feature.

**`traceparent`**

Optional valid W3C trace context on instrumented HTTP/WebSocket entry points; an invalid value is not adopted. `baggage` and `tracestate` are stripped.

 **`X-Suite-Application`**

Optional observation label, at most 128 characters from letters, digits, dot, underscore, colon or dash. Invalid labels are not recorded. This grants no permissions.

 **`X-LoadCoach-Trace` on management routes**

The same bounded label syntax groups observation metadata. Inference adds the validation and sticky-sensitivity rules below; this header does not otherwise change a management operation.

 **`X-Turnstile-No-Wake`**

Any presence prevents the proxy's wake-and-wait path when a worker is unavailable. It does not prevent model loading on an awake node, and handlers answered by the control node itself already avoid that wake path. No direct-worker effect.

### Inference {#api-inference-headers}

These headers are interpreted for local model inference; they are not general settings for every management endpoint.

**`X-LoadCoach-Priority`**

`high`, `normal` or `low`. High requires the relevant group priority or `high` ability. This orders admission, not native execution already underway.

 **`X-LoadCoach-Lock`**

Token returned by `POST /loadcoach/lock`. Also used by the explicitly marked lock, facts and load-out routes below.

 **`X-LoadCoach-Sensitivity`**

`normal`, `sensitive` or `restricted`, within the account's allowance. A trace retains its highest sensitivity; non-normal work cannot be offloaded.

 **`X-LoadCoach-Trace`**

Work identifier: 1–128 letters, digits, dot, dash, underscore or colon. Groups related calls and their sticky sensitivity; it is not a request ID.

 **`X-LoadCoach-Log: off`**

Local unlogged work only, requiring node `unlogged` support and caller permission. No other value is accepted. Cannot accompany sensitivity or trace headers, hosted work or offload. Does not erase previous records.

 **`X-LoadCoach-Require-Model-ID`, `X-LoadCoach-Require-Fingerprint`, `X-LoadCoach-Require-Build`**

Send all three together (each 1–256 characters) to pin a local model identity, configured fingerprint and engine build. A mismatch returns 409 before inference. Cannot be combined with offload or a hosted model.

 **`X-LoadCoach-Require-Model-Name`**

Require this exact local model name (1–256 characters) on any eligible node. Differently named copies, hosted models and offload are refused. The selected worker checks its local identity, settings and build again after admission. Guard refusals retain a request diagnostic link unless logging is disabled.

 **`X-LoadCoach-Offload`**

Hosted model name to consider when the local model would wait. Turnstile checks access, sensitivity and spending policy. This is permission to consider a fallback, not a forced switch; direct workers reject it.

### Hosted and plugin differences

Hosted inference implements sensitivity and trace handling; hosted requests reject unlogged work and local identity guards. Local lock and priority headers do not control a provider's queue.

For plugin aliases on `POST /v1/chat/completions`, the plugin path handles `X-LoadCoach-Sensitivity` and permitted `X-LoadCoach-Log: off`, and forwards these session controls to the selected adapter:

**`X-Suite-Session-Mode`**

`fresh` (default) or `persistent`.

 **`X-Suite-Session-ID`**

Resume the caller's adapter session in persistent mode; incompatible with fresh mode.

 **`Idempotency-Key`**

1–128 characters without control characters, scoped by adapter ownership. Reuse with different input returns 409. Not a general openWeightGym inference idempotency guarantee.

### Special and internal headers

`X-LoadCoach-Task` is a scoped external-task token, not general API access. `X-LoadCoach-Probe-ID` identifies one probe attempt of a benchmark task the control node dispatched. `X-ToolLocker-Sets` is a comma-separated tool-set filter, further restricted by caller permissions. MCP accepts `X-LoadCoach-Sensitivity` and `X-LoadCoach-Trace`; absent a trace, `Mcp-Session-Id` supplies its work grouping.

`X-MirrorWall-Job`/`X-MirrorWall-Line` and `X-SoundRoom-Job`/`X-SoundRoom-Line` associate internal batch output capture with its job/line; they are not authentication or general job-creation APIs.

`X-Suite-Statement`, `X-Suite-Client-Origin`, `X-Suite-Schedule`, `X-LoadCoach-For`, `X-LoadCoach-Role`, `X-LoadCoach-Because`, `X-LoadCoach-Role-Context`, `X-LoadCoach-From`, `X-Suite-User-ID`, `X-Suite-Instance` and `X-Suite-Model` belong to trusted inter-service or adapter traffic. A header alone cannot impersonate another user or bypass a signed statement, grant or token. Clients select roles using the JSON `model` field.

## Response headers are separate {#api-response-headers}

`X-Request-ID` and `X-LoadCoach-Request-ID` identify observed or retained requests; they are not idempotency keys. Depending on the route, responses can also include `X-LoadCoach-Model-ID`, `X-LoadCoach-Configured-Fingerprint`, `X-LoadCoach-Fingerprint`, `X-LoadCoach-Build`, `X-LoadCoach-Role`, `X-LoadCoach-Picked`, `X-LoadCoach-Because`, hosted-provider/routing metadata, advisory estimated-wait headers, and `X-Turnstile` refusal state. `Retry-After` is standard HTTP. Unlogged work has no retained request number.

`X-Suite-Proof` is worker-response verification, `X-Suite-Data-Owner` marks responses the control node answered from its own records, and adapter session/request IDs are returned for plugin work. Sending these response headers as request headers does not enable those behaviours.
