# Observability and traceability work plan

Prepared **2026-10-20** for the five requested improvements; implementation was authorized.
Status and remaining scope belong in the [agent ledger](agent-status.md).
The operating contract is in [LoadCoach](loadcoach.md#observability-and-traceability-milestone-151).
The design builds on the existing front-owned log, request diagnostics, signed node protocol
and operational alerts. It adds no monitoring daemon, external service or public port.

## Implemented changes by code section

- **Ingress and transport:** `loadcoach/observability.py` supplies task-local contexts,
  `X-Request-Id` on application HTTP outcomes and bounded W3C trace parsing. Signed Suite
  statements and request-bound private front proofs carry continuity; external correlation
  never establishes ownership. Existing `X-LoadCoach-Request-Id` remains the inference number.
  Spotter and standard-library tool hosts return IDs without creating a new history authority.
- **Execution:** routing, readiness, placement attempts, native generation and actual tool
  execution carry explicit parent spans; a missing tool response remains execution-unconfirmed. Voice turns and resumable workflow, collection and
  batch jobs have separate execution contexts; stable job identity survives retries.
- **Storage:** `loadcoach/observation_store.py` and LOG migration 7 add optional span,
  minute-histogram and source-health projections in existing owner databases. Independent
  bounded writers and existing signed node/controller forwarding preserve audit serialization.
  Explicit off-loop acknowledgement transactions remove per-record autocommit stalls; optional
  node WAL writers checkpoint in the background while required audit/front receipts retain
  FULL durability. Retention failure never counts already-committed spans as lost.
- **Reporting:** `turnstile/observations.py` exposes owner-scoped
  `/loadcoach/observations` and `/loadcoach/observation-metrics`; `see-log` permits all-owner
  reporting. Requests has an explicit trace search, JSONL metadata export, failure receipts
  and a parent timeline. Compact receipts omit account/key labels and flag their 32-span cap.
  Existing request numbers and historical pages remain valid.
- **Operations:** existing Alerts detects observation loss/backlog and optionally sustained
  application error rate/p95. Source freshness and process resets remain explicit. Rate/latency
  thresholds default to disabled until an operator establishes representative baselines.
- **Source documentation and delivery:** public helpers describe policy, side effects and
  limits; Admin Docs includes the shared tracing/reporting sources. Standalone adapters bundle
  their minimal shared dependencies. Voice store/context settings are described in
  [installation](operations.md#observability-and-traceability-milestone-151).

## Outcome and pre-implementation foundation

An application should be able to record a response identifier, find the corresponding Suite
call, and explain its routing, attempts, timing and final outcome across machines. Operators
should see when either the application or its observation pipeline is failing.

At plan preparation, the code supplied:

- `X-LoadCoach-Request-Id` for recorded inference attempts, with public machine-aware numbers.
  Some early errors and hosted connection failures omit it; unlogged inference deliberately
  has no retained request number.
- `X-LoadCoach-Trace` for grouping work, including model/tool calls, chat, workflows and batches.
  A shared value establishes correlation, not an explicit parent/child relationship.
- Compute-side queue, readiness and generation diagnostics; bounded related events and p50/p95
  summaries on request pages. Authentication, front routing and delivery are outside that timer.
- Front-owned access records, signed compute-log forwarding, a chained audit log, and durable
  alert episodes with acknowledgement and source freshness.

Keep those contracts. Historical records continue to show unavailable evidence where no
measurement or parent relationship exists. See the [current diagnostics contract](loadcoach.md#request-diagnostics-c03-milestone-92).

## Delivery order and shared design

| Item | Deliverable | Dependency |
|---|---|---|
| O1 — Consistent identification | An ingress ID on application-generated HTTP responses, linked to existing request numbers | Start here; establish O5 limits immediately |
| O2 — Complete trace timeline | Explicit service, attempt and tool relationships on the existing request page | O1 |
| O3 — Searchable structured logs | A common metadata envelope and authorized search/export | O1; use O2 relationships when available |
| O4 — Operational metrics and alerts | Windowed metrics, observation-pipeline health and actionable alert episodes | O2 and O3 |
| O5 — Bounded overhead and privacy | Enforced limits, retention, sampling and measured performance/privacy acceptance | Applies to every item; final qualification after O4 |

Front remains the authority for retained history; nodes retain their existing signed forwarding
spools. Extend the existing stores through portable migrations only where needed. Search and
metric projections must be rebuildable from retained source evidence, with explicit limits
when source retention prevents a full rebuild. Preserve audit-chain serialization and forwarding
deduplication; avoid a second account authority or scheduler.

Use [W3C Trace Context](https://www.w3.org/TR/trace-context/) for distributed identifiers and
parent relationships. [OpenTelemetry context propagation](https://opentelemetry.io/docs/concepts/context-propagation/)
provides the interoperability model for correlating spans and logs. Initially retain data through
Suite's current paths; a collector, hosted exporter or separate tracing backend is later scope.
Keep context, validation and redaction in one small shared module with framework-specific wiring.

## O1 — Consistent identification

### Work

- Generate a cryptographically random opaque `X-Request-Id` at the first Suite ingress, before
  authentication or routing. Assign a new value for each external HTTP attempt; treat any
  client-supplied value as untrusted correlation metadata rather than authoritative identity.
- Propagate the ingress ID unchanged over trusted internal hops. Carry it in the signed
  inter-service context and bind it to the request. A client cannot forge the internal context
  or borrow another user's authority through a correlation header.
- Keep `X-LoadCoach-Request-Id` unchanged as the recorded inference-attempt number. Associate
  zero or more such numbers with the ingress ID, including failed attempts before a fallback.
  Never fabricate a number for a request that was not recorded.
- Accept valid W3C `traceparent`; otherwise generate fresh context. Each internal operation
  gets its own span ID. Preserve the existing `X-LoadCoach-Trace` work-group label separately.
  Trace IDs and work labels never grant access or establish ownership.
- Return `X-Request-Id` on all application-generated HTTP outcomes, including authentication,
  validation, rate-limit, route, transport and unexpected-error responses. Cover native,
  hosted and plugin model paths and MCP HTTP ingress. Repair the missing inference-number
  header on failures that already have a retained attempt.
- Preserve the ingress ID through WeightRoom's internal form renders. Link durable background
  execution to its initiating ingress with a job-trace link; each execution attempt has fresh
  context and a stable job trace across resumes. Unrelated scheduled jobs get fresh context;
  restore task context after completion and distinguish client retries.
- Deliver IDs in initial streaming headers without buffering or delaying the body. If an
  existing browser CORS policy permits the caller, expose the relevant headers without
  widening allowed origins. A disconnected client or rejection before Suite receives the
  request cannot be promised a response header.
- Resolve authorized no-log and sensitivity policy before persisting ingress/span metadata;
  early observation stays in bounded memory until then. Failed authentication follows the
  existing independent security-audit policy rather than trusting a supplied no-log header.

### Acceptance

- A caller can log `X-Request-Id`, and a permitted operator can locate the ingress and every
  recorded inference attempt. Normal responses retain their existing number and body format.
- A response matrix covers success, 400/401/403/404/429, local/hosted/plugin 502/503, unexpected
  errors, SSE and MCP errors. Hosted connection failures retain the number already recorded.
- Concurrent users, repeated supplied IDs, forged internal context, invalid trace headers,
  internal re-renders and cancelled tasks cannot leak context or cross account boundaries.
- Unlogged inference may receive an ephemeral ingress ID but creates no searchable record,
  retained span, diagnostic log or request-attributable metric. Existing independent
  authentication/security auditing remains intact.

Primary code: `loadcoach/app.py`, `loadcoach/statement.py`, `loadcoach/client_origin.py`,
`turnstile/app.py`, `turnstile/hosted.py`, `turnstile/roles.py`, `weightroom/app.py`,
`toollocker/door.py` and plugin ingress/dispatch wiring.

## O2 — Complete trace timeline

### Work

- Add explicit parent/span identifiers for ingress, authentication, routing, wake/readiness,
  admission, model execution, each fallback/retry, tool dispatch/execution and response delivery.
  Give voice sessions/turns, workflow steps, collection operations and batch lines links to
  their actual requests. Preserve existing protocol identifiers and payload compatibility.
- Persist the minimum context required for durable jobs to resume after restart. Mark resumed
  work as a new execution attempt linked to its job; do not restore a stale in-memory span.
- Record start/end, status, sanitized error code, service, node, selected model identity/build,
  retry reason and attempt number. Distinguish a model-requested tool from a tool actually
  executed, and distinguish a final caller outcome from intermediate failed attempts.
- Use monotonic local durations. Cross-host wall-clock timestamps provide alignment only;
  show uncertainty/skew and parallel work rather than adding overlapping durations together.
  Measure first generated output separately from first response byte and proxy handoff.
- Close spans on normal completion, refusal, timeout, disconnect and cancellation. After a
  crash, label missing endings as interrupted/unknown using process-instance evidence; a
  timeout alone does not prove that remote work stopped or justify replaying a tool call.
- Extend `/requests/NUMBER` with the ingress ID, an attempt tree and linked structured events.
  Add an authorized ingress-ID lookup in Requests so pre-inference failures are findable too.
  Preserve old URLs, offline history, pagination, mobile layout and capture permissions.
- Stop timing when Suite finishes handing off the response. Receipt or processing by the
  calling application requires client evidence; it must not be inferred from an HTTP 200.

### Acceptance

- One fixture follows a failed placement, a successful fallback and a real tool execution;
  the page identifies every attempt and the caller's final outcome without double counting.
- Tests cover interleaved callers, parallel tools, hosted/plugin paths, streaming failure,
  cancellation, restart, late/duplicate node events and deliberately skewed host clocks.
- Older requests remain readable with missing fields marked unavailable. Own-request and
  `see-log` checks apply to every linked span; supplied trace IDs cannot merge authorization.
- A complete trace remains inspectable while compute is unavailable. Truncated or missing
  evidence is visible and never presented as a successful complete trace.

Primary code: `loadcoach/diagnostics.py`, `loadcoach/log.py`, `loadcoach/front.py`,
`turnstile/roles.py`, `turnstile/events.py`, `toollocker/door.py`, `toollocker/servers.py`,
`plugins/_runtime.py`, `voice/gateway.py`, `voice/service.py`, WeightRoom job orchestration
and `weightroom/pages/request*.html`.

## O3 — Searchable structured logs

### Work

- Define one versioned metadata envelope: UTC timestamp, severity, event name, service/release,
  process instance, machine, ingress/trace/span/parent IDs, attempt number, existing request
  number, stable account/key identifiers when known, application label, route template,
  outcome/error code and finite timing/count fields. Missing fields remain explicitly absent.
- Distinguish configured application identity from a client-asserted label. Validate and bound
  labels; never let them override the account, key owner or verified client IP. Correlation
  metadata sent to outside providers must follow an explicit allowlist.
- Attach context to Suite-owned Python logs and use structured journal output for service
  diagnostics. Existing chained events remain the audit authority; searchable projections
  link to their event IDs. Label native/third-party output as uncorrelated unless a real
  request link is available; do not infer ownership from time proximity alone.
- Add exact-ID lookup and bounded filters for time, service, machine, application, outcome
  and error code. Reuse the existing front reporting deadline/read-only connection boundary;
  index the actual lookup fields and paginate before returning results.
- Provide authorized JSON/JSONL metadata export and a compact copyable failure receipt with
  IDs, version, route, attempts, timings and error code. Escape display values and redact
  secrets before persistence, journal emission and export.
- Link request failures to matching structured events and the existing permission-protected
  service journal view. Keep raw journal access and captured model content under their current
  permissions; a broad log search must not make them public.

### Acceptance

- A single ingress ID finds the complete authorized event set across front and nodes;
  a user cannot search/export another user's events by guessing any identifier.
- Secret-canary fixtures place credentials, cookies and prompt/tool content in headers,
  exceptions and URLs; none enter the new metadata fields, journals or exports.
- Large retained histories meet the reporting deadline without a request-path full-table scan.
  Duplicate/replayed events do not create duplicate attempts or inflate summaries.

Primary code: `loadcoach/log.py`, `loadcoach/front.py`, `turnstile/events.py`,
`turnstile/reporting.py`, `db/schemas.py`, app logger wiring and WeightRoom log/request views.

## O4 — Operational metrics and alerts

### Work

- Extend existing operator views with fixed-window counters and histograms: inbound calls,
  completed attempts, final outcomes, concurrency, queue/readiness durations, end-to-end
  service latency, observed first output, token throughput, fallback and disconnect rates.
  Count logical calls and inference attempts separately and label their denominators.
- Aggregate histogram counts across nodes; never average node p95 values. Display window,
  sample count, freshness and approximate-versus-exact percentile basis. Exclude benchmarks
  by default and preserve the current distinction between refusals, cancellations and failures.
- Limit metric dimensions to configured services, nodes, model identities, bounded route
  templates and outcome/error classes. Keep request/trace IDs, IPs, arbitrary URLs and
  caller-supplied labels out of metric dimensions. Account-level views remain authorized.
- Expose observation health: forwarding backlog count/age, last successful acknowledgement,
  dropped optional diagnostic records, writer errors, incomplete spans and unavailable nodes.
  Include process-instance/reset metadata so a restart cannot look like a negative counter.
- Extend the existing Alerts collector and durable episodes with configurable sustained
  error/latency, backlog and observation-loss conditions. Apply minimum sample counts,
  debounce/recovery windows and deduplication; acknowledgement does not imply recovery.
  Missing/stale evidence remains unknown. Long-lived active streams are not failed merely
  because they exceed an ordinary request-duration threshold.
- Start pipeline-health alerts enabled; establish application latency/error thresholds from
  a representative measured baseline before enabling those alerts. Reuse existing in-app
  notifications; outbound email/webhooks are outside this increment.

### Acceptance

- Deterministic fixtures verify counts, denominators, histogram merge, reset/restart behavior,
  benchmark exclusion and missing-data handling. Reconcile sampled dashboard totals against
  retained evidence and disclose sampling/truncation instead of implying exact full coverage.
- Simulated node/front outages, delayed forwarding, writer failure and queue saturation
  produce one useful alert episode, with source freshness and a link to relevant evidence.
- Recovery requires positive fresh evidence; low traffic and deliberate cancellations do
  not generate misleading error-rate or latency incidents.

Primary code: `weightroom/alerts.py`, its existing collector in `weightroom/app.py`,
`loadcoach/front.py`, `loadcoach/log.py`, `turnstile/events.py`, `turnstile/reporting.py`,
`db/schemas.py` and existing request/alert/report views.

## O5 — Bounded overhead and privacy

### Work and initial budgets

These implementation budgets apply throughout the work. Measured qualification is linked from
the operating contract; the baseline precedes the changes.

| Boundary | Initial budget or rule |
|---|---|
| Header/context input | Parse bounded standard headers; cap optional Suite labels at 128 characters; strip unapproved baggage and secrets |
| Detailed trace | At most 256 spans and 64 KiB serialized optional metadata per ingress request; record a truncation flag |
| Optional diagnostic queue | Cap at both 4,096 records and 8 MiB per process; aggregate dropped counts by reason |
| Flush | Batch optional writes at 100 records or 1 second; bounded shutdown flush of 2 seconds |
| Retention | New detailed span/index data: 7 days; minute metric rollups: 30 days; respect existing shorter source retention and audit retention contracts |
| Reporting | Paginated results, existing independent report deadline and indexed ID lookups |
| Performance acceptance | At matched load, no more than 5% throughput loss; added p95 latency no greater than the larger of 2 ms or 5% of baseline; no steady memory growth beyond declared buffers |

- Separate optional diagnostic buffering from required audit writes. A failed optional writer
  must not block inference; security-sensitive audit requirements retain their existing
  fail-closed behavior. Do not acknowledge undurable audit records or discard unsent node
  audit evidence merely to satisfy a diagnostic queue budget.
- Begin with bounded detailed recording enabled. Under pressure, sample successful detailed
  spans first; retain minimum completion/error metadata within the bounded queue and expose
  any loss. Do not promise lossless errors during disk failure or an attack. Counters must
  distinguish optional sampling from actual data loss.
- Keep IDs/context in task-local storage, restore it on exit, and explicitly propagate only
  approved fields to child tasks and durable jobs. Do not buffer uploads, tokenize responses
  or add per-token/per-chunk database writes to collect traces.
- Exclude raw keys, credential verifiers, cookies, full query strings, prompts, responses and
  tool arguments/results from new ordinary telemetry. Keep verified IP/account access data
  in its authorized registry; avoid duplicating it into broadly readable service logs.
- Preserve the existing unlogged-inference contract, including retained aggregates: no new
  anonymous usage counting is introduced. Existing infrastructure resource readings and
  independent security audits remain subject to their current policy.
- Perform bounded retention cleanup through existing lifecycle/maintenance paths. Never delete
  authoritative audit events independently of the chain's retention contract. A sampled or
  expired trace must remain visibly incomplete when its inference record survives.
- Document every new function/class and public field in source; include side effects,
  cancellation, authorization and limits. Update source-derived Docs and operating guides.

### Acceptance

- Compare disabled/enabled instrumentation using warmed, alternating runs at concurrency 1,
  8 and 32 where supported: cheap API calls, CPU-model calls, SSE, tool calls and rejected
  credentials. Record offered load, model/build, payload size and p50/p95/throughput/memory;
  separate cold starts. Repeat noisy measurements before declaring a regression or a pass.
- Slow/full disks, prolonged front outage, saturated queues and shutdown prove bounded memory,
  bounded flushing, visible diagnostic loss and unchanged required-audit failure behavior.
- Security tests cover context forgery/replay, malformed headers, secret redaction, permission
  isolation, no-log behavior and hostile high-cardinality labels. The existing source-docstring
  and documentation checks pass.

## Verification and rollout

For each item, add focused behavioral tests alongside existing request diagnostics, routing,
hosted/plugin, tools, alerts and forwarding tests. Run the full suite after the integrated
change, including isolated PostgreSQL schema/repository contracts when available; disclose
environment-dependent skips. Check JavaScript syntax and desktop/mobile request/log views.

When implementation is authorized and ready:

1. Review all dirty-checkout dependencies, freeze an immutable source release, inventory every
   configured node and installed adapter/tool-host copy, and verify no queued/running benchmark
   work before affected restarts. Drain active work and preserve its implementation identities.
2. Validate portable schema upgrades, existing links/history and fresh-install behavior in
   disposable fixtures. Update installation/defaults/config compatibility documentation when
   those surfaces change. Live database engine bindings remain unchanged.
3. Activate matching source overrides and affected services across front, compute and remote
   CPU hosts. Preserve private configuration and existing inactive-service state. Use a source
   release compatible with any new schema for recovery; do not point old writers at a newer
   schema. The owner's current disposable-install backup waiver remains as recorded in the
   [sweep deployment requirements](application-sweep-2026-10-20.md#deployment-requirements).
4. From a real calling machine, verify success and failure response IDs, exact authorized
   lookup, native/hosted/plugin and tool relationships, streaming/disconnect outcomes and
   observation-health reporting. Use fixtures for destructive/outage scenarios; do not perform
   production power tests or interrupt unrelated work.
5. Reconcile retained records, service/source versions and observation loss counters; restore
   worker/drain state and revoke temporary validation credentials. Put detailed measurements
   in a machine-readable qualification artifact and link it from the owning guide.

Completion requires all five acceptance gates, source documentation, matching delivery and
independent live verification. Update approved milestone state in README when implementation
is selected, remove completed execution work from Progression, and move the final operating
contracts into the existing guides. Preserve recovery/compatibility instructions and evidence;
do not turn this plan into a second historical status log. The owner's later full clean
reinstall remains a separate acceptance step.
