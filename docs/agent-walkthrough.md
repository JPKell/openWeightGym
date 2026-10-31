# openWeightGym agent walkthrough

**Reviewed:** 2026-10-31. **Execution status: Issue — bounded local run completed; live and owner gaps remain.** This file identifies what an agent can test from the [app walkthrough](../app-walkthrough.md). All 96 page/state entries are represented below. The manual walkthrough owns page instructions and findings; this companion owns test methods, execution boundaries and coverage gaps.

Read the [agent reference](agents.md), [current delivery/acceptance status](agent-status.md) and [existing audit findings](../app-walkthrough.md#functionality-and-usability-audit) first. Recheck those findings against the current source before treating them as open or fixed. Do not copy old results into a new run or overwrite the owner's checklist.

## What an agent can test

| Mode | What it establishes | Boundary |
|---|---|---|
| **L — Local automated** | Real application routes/templates/scripts against isolated accounts, stores, fake engines, synthetic audio and controlled failures. Can test mutations, refusal, cancellation, restoration and cleanup without changing the installation. | A mock response proves client behavior, not actual inference, hardware, a provider or a deployed release. Use temporary stores and CPU-only Chromium (`--disable-gpu`). |
| **R — Live read-only** | Current authenticated navigation, page/data rendering, permitted detail links, saved evidence, settings presentation and passive availability. | Use the authorized session/account. Follow only read-only controls; a page containing actions does not authorize those actions. Record active source revisions separately from the local checkout. |
| **W — Conditional live work** | A small real conversation, media job, collection/workflow step, API call or explicitly selected scratch-data experiment. | Requires available permitted models/services and a bounded run scope. Check queued/running benchmarks, active requests, resident models and memory first; preserve others' work. If a required load would evict protected/current work, mark that check Blocked and continue independent checks. No new paid provider use without its explicit scope. |
| **H — Owner/device evaluation** | Physical microphone/speaker/headset behavior, a real telephone call, native OS setup and subjective recognition, voice/image/music quality or usefulness. | The agent can prepare samples, inspect UI and measure protocol stages. It cannot infer physical or subjective acceptance from simulation. Existing [Voice acceptance](agent-status.md#voice-acceptance) remains recorded; do not reopen completed gates automatically. |
| **M — Optional maintenance** | Deliberate outages, restarts, deployment, power, migrations, restoration, credential/machine-key rotation or destructive shared-storage cleanup. | Separate from the routine walkthrough. Follow the existing operation/recovery contract and current authorization. A routine test request does not authorize waking/suspending/rebooting hosts, a live engine cutover or a clean reinstall. |

Modes in the page tables describe available methods, not execution results. Within a mixed page, run L/R checks first; only the specific work/control described belongs to W, H or M.

## Prepare and record a run

- [ ] Record date, checkout commit/dirty files, active release(s), browser/version, viewport, timezone, account role and test mode. Preserve unrelated work; prefix shell commands with `rtk`.
- [ ] Inventory available tools, browser/session access and local test dependencies. Use existing isolated test fixtures; never point a test's temporary database or fake-engine configuration at production.
- [ ] For live checks, establish the permitted origin/account and current availability without exposing credentials. If sign-in or browser certificate handling requires the owner, record the affected live checks as Blocked and continue locally.
- [ ] Use separate administrator/restricted sessions where available. Record expected denials as Pass when they are the purpose of the check; missing access needed for a positive check is Blocked.
- [ ] List owned scratch objects before mutations and set a small work/time/output limit. Do not modify another account, profile, model, schedule, request, asset or provider configuration merely to exercise a form.
- [ ] Begin with Voice, then shared controls, then every page table below. Test detail pages, tabs and dynamic states as well as landing pages.

Use **Not run**, **Pass**, **Issue**, **Blocked** or **Not applicable** for each case. Not applicable needs a reason tied to the chosen scope; unavailable hardware/models are usually Blocked. A page that loads is not a pass for every action on that page. A reproduction test passing because it demonstrates a defect records **Issue** for the product.

Store new run evidence under a private/local `data/qualification/agent-walkthrough/<run-id>/` directory. This directory is ignored by Git; do not depend on its presence in another checkout. Keep a JSON result per case with these fields:

```json
{
  "case_id": "chat-voice/startup-cancel",
  "manual_anchor": "chat-voice",
  "mode": "L",
  "status": "Not run",
  "source_revision": "",
  "environment": {"browser": "", "viewport": "", "timezone": "", "role": ""},
  "steps": [],
  "expected": "",
  "actual": "",
  "evidence": [],
  "finding_reference": "",
  "blocked_reason": "",
  "cleanup": ""
}
```

Evidence may include a test node ID, sanitized request/response metadata, screenshot or captured action target. Exclude keys, cookies, passwords, private prompts/files, raw recordings and credential-bearing URLs. Store screenshots of private content only when needed and permitted. Write findings once in the [manual findings area](../app-walkthrough.md#findings-to-act-on), linking the receipt; reference existing finding IDs instead of duplicating them here.

## Start with Voice

- [ ] Compare CPU voices, saved voices and Hermes with the profiles/modes actually offered. Verify profile defaults and explicit overrides, compatible model/voice choices, and a useful empty-state path to Speech → Voices.
- [ ] Delay/fail configuration and session reservation locally. Test early Start, startup Cancel, late responses, microphone refusal, unsupported echo cancellation, empty compatible modes/profiles and repeat connection attempts.
- [ ] With synthetic audio, exercise mute/silence, finish-speaking, interruption, disconnect/reconnect, transcript ordering, playback progress, cleared queues and retained error reasons. Confirm cleanup releases capture, sockets, sessions and acquired holds.
- [ ] Verify recording opt-in, account ownership and revocation using isolated identities/assets. Confirm Direct and Hermes retain separate configuration/history; keys stay server-side.
- [ ] Compare browser, gateway, speech worker and desktop package source versions before a live claim. For W calls, use generated non-private input and explicit limits; record model, engine, profile and protocol evidence. Leave physical echo, speaker stop and voice quality to H.

Maintained tests: [browser](../tests/test_voice_browser.py), [service](../tests/test_voice.py), [gateway](../tests/test_voice_gateway.py), [Hermes](../tests/test_voice_hermes.py), [echo](../tests/test_voice_echo.py), [latency](../tests/test_voice_latency.py), [administration](../tests/test_voice_admin.py), [greetings](../tests/test_voice_prompt_recordings.py), [desktop transport](../tests/test_voice_desktop.py), [desktop UI](../tests/test_voice_desktop_ui.py). Native UI tests may require an isolated display; do not take over the owner's desktop to run them.

## Shared checks on every page

- [ ] Verify the expected title, active navigation, visible/hidden controls by role, deep links, browser Back/Forward and mobile Menu. Follow the app's actual links for dynamic object IDs.
- [ ] Wait for initial data and dynamic controls; capture JavaScript errors, failed requests and redirects. An empty shell or placeholder before data settles is not the final page.
- [ ] Test 1440 px and 390 px widths, then 320 px where relevant, light/dark appearance, keyboard focus, labels, dialogs and long/empty/missing values. Use synthetic values for injection/escaping checks.
- [ ] Test sorting/filtering/pagination, selected text, open cards and focused inputs through polling/SSE replacement. Check chronological sorting across a month/year boundary and numeric sorting with formatted values.
- [ ] Test browser-local times in two zones while preserving UTC recovery values; inspect units, whole counts and unavailable versus zero readings.
- [ ] In L, delay mutations to test progress feedback, duplicate prevention, failed-form restoration and timeout/retry behavior. Snapshot action IDs before confirmation and verify Cancel sends no mutation.
- [ ] In L, fail one polling/stream region while another stays healthy. Check independent recovery, expired-session navigation and preservation of drafts/streamed content.
- [ ] Test 403/404/500 and missing-object states in fixtures; do not force a live server exception or service outage for a screenshot.

Start from [shared UI checks](../tests/test_ui_consistency.py), [console interactions](../tests/test_console_browser.py), [polling/selection](../tests/test_polling_selection.py), [form restoration](../tests/test_forms.py) and [node availability](../tests/test_node_availability_browser.py). Extend a gap with a small isolated reproduction when necessary; the existing tests do not automatically cover every row/action below.

## Home

| Page / manual checklist | Agent checks | Modes / limits |
|---|---|---|
| [Home](../app-walkthrough.md#home-home) | Permitted model links, current/stale availability, usage period/scope, chart/table totals and request links; compare restricted and summary-enabled accounts. | L/R; W only to add a bounded known request. |

Tests: [dashboard](../tests/test_dashboard.py), [usage](../tests/test_usage.py).

## Chat

| Page / manual checklist | Agent checks | Modes / limits |
|---|---|---|
| [Conversations](../app-walkthrough.md#chat-conversations) | Visible model/Send controls, no-model state, history/search, rename, ordering and account privacy; retain draft/navigation context. | L/R; W for owned scratch history changes. |
| [Conversation](../app-walkthrough.md#chat-conversation) | Stream/follow-up, Stop/retry/Continue, copy, settings persistence, attachments, comparison, temporary and plugin session modes; inspect actual submitted model/options. | L/R; W for real replies; no existing conversation edits. |
| [Collections](../app-walkthrough.md#chat-collections) | Owner list, sensitivity, embedder selection, create validation and no-permitted-embedder state. | L/R; W for a disposable collection. |
| [Collection](../app-walkthrough.md#chat-collection) | Text/PDF upload and original download, unsupported/scanned PDF handling, indexing progress/cancel/retry, embedding identity, privacy and absent-fact retrieval. | L/R; W for indexing/search/generation. Rebuild/delete only disposable sources. |
| [Source excerpt](../app-walkthrough.md#chat-source-excerpt) | Citation-to-original correspondence, surrounding text, collection ownership, long text and missing/forbidden source links. | L/R; generated citations require W if none exist. |
| [Workflows](../app-walkthrough.md#chat-workflows) | Creation validation, model/output choices, prompt version preservation and frozen source/model identity. | L/R; W for owned scratch runs. |
| [Workflow](../app-walkthrough.md#chat-workflow) | State transitions, research acceptance, draft/review, attempt records, cancellation/retry, reopening and exact citations. | L/R; W for real steps; acceptance here tests state, not owner approval of prose. |
| [Image generation](../app-walkthrough.md#chat-image-generation) | Model/workflow constraints, references/mask wording, delayed submission/polling, exact deletion target, gallery/reuse/download and temporary mode. | L/R; W for a small job. Reproduce unsafe deletion locally; do not test an open deletion defect on live assets. H for quality. |
| [Music](../app-walkthrough.md#chat-music) | Model-specific controls, lyrics, reuse/variation seed/source/range, loop, older/tagged audio, failed jobs and retry payloads. | L/R; W for short takes; H for musical quality. |
| [Sounds](../app-walkthrough.md#chat-sounds) | Sound-specific controls, progress/cancel, playback/download, reuse and owned deletion target. | L/R; W for short output; H for sound quality. |
| [Speech](../app-walkthrough.md#chat-speech) | Every tab: script/speaker/default restoration, loops, recording libraries, transcript/copy/export selection races, voice consent/share and spoken-chat link parameters. | L/R with generated clips; W for synthesis/transcription/clone/design; H for voice quality and real microphone use. |
| [Voice](../app-walkthrough.md#chat-voice) | Run the Voice-first checklist above; inspect permissions, scoped call options and session cleanup. | L/R; W for a real generated-audio exchange; H for physical microphone/output. |
| [Voice desktop app](../app-walkthrough.md#chat-voice-desktop-app) | Download/ZIP contents, launch instructions, absence of private credentials, saved-key handling and simulated device/socket cleanup. | L/R; H for native Windows/macOS/Linux setup and physical audio. |

Tests: [front Chat](../tests/test_front_chat.py), [Chat controls](../tests/test_chat_controls.py), [restart](../tests/test_chat_restart.py), [scroll](../tests/test_chat_scroll_browser.py), [collections](../tests/test_collections.py), [collection pages](../tests/test_collection_pages.py), [indexing status](../tests/test_collection_status_browser.py), [workflows](../tests/test_workflows.py), [workflow pages](../tests/test_workflow_pages.py), [images](../tests/test_mirrorwall.py), [image controls](../tests/test_model_image_settings_browser.py), [audio](../tests/test_soundroom.py), [audio browser](../tests/test_soundroom_browser.py).

## Models

| Page / manual checklist | Agent checks | Modes / limits |
|---|---|---|
| [Models](../app-walkthrough.md#models-models) | Catalogue/type pills, Add expansion, permitted entries, node/file selection, copy/capacity refusal and exact detail links. | L/R; W for a disposable entry/transfer or real execution. |
| [Model](../app-walkthrough.md#models-model) | Language/image/audio variants; per-node versus global settings, preview/apply/preset/undo, compatibility, identity/history and offline refusal. | L/R; configuration changes/load/add-to-node only within W scratch scope; preview is not load proof. |
| [Images](../app-walkthrough.md#models-images) | Family/components, cache/storage file choice, missing/incompatible companions, node placement/RAM and shared defaults/limits. | L/R; W for an isolated disposable entry; no edits to an in-use model. |
| [Image workflows](../app-walkthrough.md#models-image-workflows) | Create/version/reopen a scratch workflow, preserve prior versions, model-specific fields and studio application. | L/R; W for live saved workflow changes; generation separately gated. |
| [Audio models](../app-walkthrough.md#models-audio-models) | Runtime-declared kind/family, complete package/companions, licence guidance, defaults, placement and unsupported runtime errors. | L/R; W for scratch entry changes; package install/download needs a bounded target. |
| [Dry fit](../app-walkthrough.md#models-dry-fit) | Type/node filtering preserves selection; measured/estimated/unknown memory, Auto fit, context, headroom, saved-set preview and undo. | L/R; W for apply/load/hold/renew/release on a fitting test set; never unload someone else's model. |
| [Roles](../app-walkthrough.md#models-roles) | Candidate access/capabilities/context, quality weights, missing/stale evidence, residency preferences and preview explanation. | L/R; W for a disposable role/request and actual selection evidence. |
| [Files](../app-walkthrough.md#models-files) | Per-node roots, size/type/use/duplicates, metadata/actions and transfer/promotion progress; storage originals remain. | L/R; W for bounded scratch copy/move; M for shared cleanup. |
| [File](../app-walkthrough.md#models-file) | Supplied node/path, metadata/use and type-appropriate model/adapter/projector links; protected/in-use refusal. | L/R; deletion/copy/move only owned scratch targets, cleanup under M. |
| [HuggingFace](../app-walkthrough.md#models-huggingface) | Search/repository/file/destination UI and missing/private/collision errors; interrupted/resumed progress in fixtures. | L/R; W for an explicitly chosen small download; don't run a downloaded model/code as a UI check. |
| [Civitai](../app-walkthrough.md#models-civitai) | Model/version/file details, safe preview policy versus personal opt-in, destination validation and transfer status. | L/R; W for a chosen small transfer; global policy changes use fixtures. |
| [Fingerprints](../app-walkthrough.md#models-fingerprints) | Current/historical hash/configuration identity and cross-links from model/request/run. | L/R; W only if creating new comparison evidence. |
| [Cloud APIs](../app-walkthrough.md#models-cloud-apis) | Offered names/prices/ceilings, masked key status, failed-secret submission handling, mapping and hosted/sensitivity denial. | L/R with mock keys/providers; real credentials/spend require their specific scope. |

Tests: [setup](../tests/test_model_setup_browser.py), [installation](../tests/test_model_install.py), [install browser](../tests/test_model_install_browser.py), [image/settings](../tests/test_model_image_settings_browser.py), [saved sets](../tests/test_loadouts_browser.py), [roles](../tests/test_roles_browser.py), [hosted controls](../tests/test_console_browser.py).

## Benchmark

| Page / manual checklist | Agent checks | Modes / limits |
|---|---|---|
| [Runs](../app-walkthrough.md#benchmark-runs) | Default/alias navigation, eligible language targets, queue/progress/history, exact cancellation target and completed-sample retention. | L/R; W for one selected run and foreground-preemption check, preserving prior pause state. |
| [Run detail](../app-walkthrough.md#benchmark-run-detail) | Exact protocol/model/node/config identity, state/caps, score/sample evidence, attempts and log/request links. | L/R; W only when a new sample is needed. |
| [Results](../app-walkthrough.md#benchmark-results) | Type/benchmark retention, score/heatmap direction/order, missing/stale distinctions, historical identity and exact run links. | L/R; don't rerun the catalogue for page coverage. |
| [Node measurements](../app-walkthrough.md#benchmark-node-measurements) | LLM/Embedder/Image/Audio pills, Speed retention, host columns, units and unavailable versus zero. | L/R from retained or fixture measurements. |
| [Embedders](../app-walkthrough.md#benchmark-embedders) | Context/speed/load views, CPU/GPU locations, vector/input-capacity evidence and saved run comparison. | L/R; W for one bounded real measurement if needed. |
| [Compare](../app-walkthrough.md#benchmark-compare) | Multi-selection, fingerprints/engine/machine filters, score direction, missing evidence and exact source runs. | L/R; H for usefulness to the owner's actual workload. |
| [Manage](../app-walkthrough.md#benchmark-manage) | Versioned case import/export, invalid JSON/checks, retained input, formula validation and immutable dataset identity. | L/R; mutations use isolated definitions, not shared production formulas/enablement. |

Tests: [benchmark pages](../tests/test_benchmark_pages.py), [browser](../tests/test_benchmark_browser.py), [custom cases](../tests/test_custom_benchmarks_browser.py). A checker fixture pass is not model quality qualification.

## Logs

| Page / manual checklist | Agent checks | Modes / limits |
|---|---|---|
| [Usage](../app-walkthrough.md#logs-usage) | Known-request totals, date/zone/scope filters, views, cumulative/exact tables, mock versus recorded costs and bookmarks. | L/R; use synthetic amounts for boundary cases. |
| [Spending allowances](../app-walkthrough.md#logs-spending-allowances) | Scope/period/coverage, soft-policy wording, missing costs and persistence/validation on a test target. | L/R; actual spending-policy changes require a selected scratch scope. |
| [Requests](../app-walkthrough.md#logs-requests) | Column filters/ranges/paging, URL retention, client attribution, trace search/export, metrics/freshness and missing legacy evidence. | L/R; W to produce one known request if necessary. |
| [Request detail](../app-walkthrough.md#logs-request-detail) | Live/completed/error/cancelled variants, caller/model/location, timings and related events/output/journal; permissions and feedback. | L/R; W for feedback or Stop on owned scratch work only. |
| [Batches](../app-walkthrough.md#logs-batches) | API-submitted item progress, result/error downloads, leaving/reopening and completed output after cancellation. | L/R; W for a tiny owned batch; never Stop all other work. |
| [AI Logs](../app-walkthrough.md#logs-ai-logs) | Filters/paging, known action links, local times, long/empty values and account visibility. | L/R; avoid copying private logged content into receipts. |
| [Log entry](../app-walkthrough.md#logs-log-entry) | Attribution/related evidence, request-event canonical redirect and missing/forbidden recovery. | L/R; Ask a model is W and sends selected output to that model. |
| [Reliability](../app-walkthrough.md#logs-reliability) | Model/role filters, chart/table evidence, reasons/dates, exclusion and recovery presentation. | L/R; reset/recovery mutations use fixtures or an M test target. |
| [Plugin logs](../app-walkthrough.md#logs-plugin-logs) | Instance/model correlation, outcome/timing/filtering and scoped private detail. | L/R; W only to create known plugin activity. |
| [System logs](../app-walkthrough.md#logs-system-logs) | Service/time/context selection, request-centred links, unavailable output and persistent denial without `restart-services`. | L/R; selecting a service for logs must not restart it. |

Tests: [usage](../tests/test_usage.py), [allowances](../tests/test_allowances.py), [request filters](../tests/test_request_filters.py), [diagnostics](../tests/test_request_diagnostics_pages.py), [front logs](../tests/test_log_front.py), [reliability pages](../tests/test_reliability_pages.py), [plugins](../tests/test_plugin_front.py).

## System

| Page / manual checklist | Agent checks | Modes / limits |
|---|---|---|
| [Status](../app-walkthrough.md#system-status) | Every node, resident placement/memory/queue, freshness, selector scope and owned request correlation. | L/R; W for a small test hold/lock/unload only when it affects no existing work; drain/clear under M. |
| [Telemetry](../app-walkthrough.md#system-telemetry) | Physical-host grouping, top-meter correspondence, missing readings and chart ranges/units/tooltips. | L/R; no load/power transition required. |
| [Physical host telemetry](../app-walkthrough.md#system-physical-host-telemetry) | Every configured host, grouped instances, retained history, Hours/Until/zoom and local axes/UTC tooltips. | L/R; inspect Wake without using it. |
| [Machine](../app-walkthrough.md#system-machine) | Allowed service inventory, action target, interruption wording, draining/idle explanation and restricted controls. | L/R; service/power actions are M; physical power needs separate authorization. |
| [Voice diagnostics](../app-walkthrough.md#system-voice) | Readiness/mode/profile meanings, current versus cold models, recent numeric stages/percentiles, missing acknowledgements and settings link. | L/R; W to generate a numeric turn; physical latency H. Preview/greetings are on Settings → Voice. |
| [Alerts](../app-walkthrough.md#system-alerts) | Seeded open/resolved/acknowledged episodes, source freshness, thresholds and detail links; empty list isn't lifecycle coverage. | L/R; acknowledgement only an explicitly selected test episode; outages in L. |
| [Schedules](../app-walkthrough.md#system-schedules) | Backup/benchmark/outstanding scopes, ownership, timezones/DST, missed/overlap/offline policy and next occurrence/history. | L/R; create/edit/pause/resume/delete isolated schedules; real recurring work must be bounded and explicitly selected. |
| [DB](../app-walkthrough.md#system-db) | Store/engine/table/schema/health/backup-age presentation and export round-trip with synthetic records. | L/R; live exports contain private data and require a suitable selected scope; engine changes M. |
| [Database migration preview](../app-walkthrough.md#system-database-migration-preview) | POST-rendered preflight/report, source/target bindings, tools/space and chain/artifact/recovery checks. | L using isolated profiles; live preview may probe resources; migration/cutover M. |
| [Backups](../app-walkthrough.md#system-backups) | Verified versus partial/missing recovery sets, front/compute parts, sizes, retention and exact restore-preview links. | L/R; actual backup/identity bundle creation isn't needed for page coverage; live restore/import M. |
| [Restore preview](../app-walkthrough.md#system-restore-preview) | Selected manifest/parts/files/exclusions, owning services, restore order and missing-half refusal. | L/R; never execute restore as part of preview verification. |
| [Deploy](../app-walkthrough.md#system-deploy) | Superuser visibility, checkout versus active pin, node reachability and retained job states/output. | L/R; Pull/Set up/Deploy/Restart/Add host under M, not browsing. |

Tests: [status](../tests/test_status_nodes.py), [telemetry](../tests/test_machine_telemetry.py), [alerts](../tests/test_alerts.py), [schedules](../tests/test_schedules.py), [DB administration](../tests/test_database_admin.py), [migration](../tests/test_database_migration.py), [backups](../tests/test_backups.py), [deployment](../tests/test_deploy.py). PostgreSQL-specific checks need an isolated test DSN; missing it is Blocked, not a live engine-change request.

## Settings

| Page / manual checklist | Agent checks | Modes / limits |
|---|---|---|
| [Compute nodes](../app-walkthrough.md#settings-compute-nodes) | Node/engine/GPU/CPU-RAM scope, file-only/restart status, validation and restored values in fixtures; rotation guidance. | L/R; selected reversible edits only; runtime/key changes under M. |
| [Users](../app-walkthrough.md#settings-users) | List/filter/sort/paging, enabled states, Add validation, selected group and exact detail links. | L/R; use isolated users for creation/access changes. |
| [User](../app-walkthrough.md#settings-user) | Inherited/own permissions/model/sensitivity limits, restricted session, rename/history, credentials, disable/re-enable and grant ceilings. | L/R; identity/security mutations use isolated accounts; no owner credential replacement. |
| [Groups](../app-walkthrough.md#settings-groups) | List/members/limits, restricted view, Add validation and selected detail. | L/R; creation in isolated stores by default. |
| [Group](../app-walkthrough.md#settings-group) | Effective policy, assignment, model/action denials, membership restoration and last-administrator protection. | L/R; security/membership changes use isolated groups/accounts. |
| [Tools](../app-walkthrough.md#settings-tools) | Server/schema/level/toolset presentation, tester arguments, harmless mock result/error and direct-call denial. | L/R; live tool execution only a known read-only permitted tool on non-private scratch input. |
| [Plugins](../app-walkthrough.md#settings-plugins) | Valid/invalid plugin entries, catalogue versus running state, auth/readiness notices and detail links. | L/R; no installer execution or adapter start implied. |
| [Plugin](../app-walkthrough.md#settings-plugin) | Manifest/capabilities, instance list, enablement state and Add navigation. | L/R; in-use enablement changes under M. |
| [New plugin instance](../app-walkthrough.md#settings-new-plugin-instance) | Required/invalid fields, retained values, instance ID/address/sensitivity and pending versus applied settings. | L/R form inspection; creation in L. Live disposable instances need a cleanup plan because no delete control exists. |
| [Plugin instance](../app-walkthrough.md#settings-plugin-instance) | Scoped settings, profiles/aliases, health/discovery results, synchronization and installer package privacy. | L/R; live health/discovery contacts its configured adapter; restart/install under M. |
| [Voice settings](../app-walkthrough.md#settings-voice) | Service/gateway profile separation, model/speaker choices, application timing, synthetic preview, greeting cache and pending-change refusal. | L/R; live preview uses CPU synthesis; changes/regeneration use an explicitly selected profile/window; restart under M. |
| [Audio](../app-walkthrough.md#settings-audio) | Global versus personal defaults, unavailable/file-only models and restart/application notices. | L/R; mutate defaults in fixtures; no restart just to apply a test. |
| [Image](../app-walkthrough.md#settings-image) | Administrator global policy, separate default-off personal opt-in, safe-preview restoration and editor links. | L/R; exercise policy changes with isolated users/configuration. |
| [General](../app-walkthrough.md#settings-general) | Purpose/service grouping, serial save/partial refusal, current/pending/restart values, secrets and per-node roots. | L/R; preserve selected settings before reversible experiments; storage roots and runtime changes M. |

Tests: [forms](../tests/test_forms.py), [settings](../tests/test_settings.py), [host settings](../tests/test_host_settings.py), [user rename](../tests/test_user_names.py), [tool access](../tests/test_tool_access_pages.py), [plugins](../tests/test_plugins.py), [plugin pages](../tests/test_plugin_front.py), [console](../tests/test_console_browser.py).

## Docs

Apply the shared page checks to every topic row: map/breadcrumb/topic/module links, active selection, escaped source, signatures/line numbers, no execution/import of displayed code, restricted access and mobile readability. Source selections are views within a page, not extra routes to count.

| Page / manual checklist | Specific agent comparison | Modes |
|---|---|---|
| [User guide](../app-walkthrough.md#docs-user-guide) | Ordinary-account access, task help anchors, current terminology/actions and configured connection addresses. | L/R |
| [System reference](../app-walkthrough.md#docs-system-reference) | Ownership map, front/worker boundaries and topic directory links. | L/R |
| [API reference](../app-walkthrough.md#docs-api-reference) | Implemented methods/paths/headers, unsupported APIs, handler links and credential-free examples. | L/R |
| [WeightRoom](../app-walkthrough.md#docs-weightroom) | Console/chat/admin/appearance modules and current page structure. | L/R |
| [Turnstile](../app-walkthrough.md#docs-turnstile) | Authentication, routing, placement and readiness ownership. | L/R |
| [LoadCoach](../app-walkthrough.md#docs-loadcoach) | Admission, residency and inference lifecycle sources. | L/R |
| [ToolLocker](../app-walkthrough.md#docs-toollocker) | Discovery/MCP/permissions/execution references. | L/R |
| [Voice](../app-walkthrough.md#docs-voice) | Gateway, worker, client and administration source coverage. | L/R |
| [Spotter](../app-walkthrough.md#docs-spotter) | Health/telemetry/alert/power references; inspect rather than execute power actions. | L/R |
| [Hosted providers](../app-walkthrough.md#docs-hosted-providers) | Routing/credential/cost/limit contracts; no real key in examples. | L/R |
| [Plugins](../app-walkthrough.md#docs-plugins) | Adapter install/runtime/activity/recovery source links. | L/R |
| [Logs](../app-walkthrough.md#docs-logs) | Capture scope, tracing, diagnostics and reliability references. | L/R |
| [Models](../app-walkthrough.md#docs-models) | File/preset/role/projector/embedder source links. | L/R |
| [Mirror Wall (images)](../app-walkthrough.md#docs-mirror-wall-images) | Supported families/components, reference/mask/memory limits versus current editors. | L/R |
| [Benchmarks](../app-walkthrough.md#docs-benchmarks) | Versioned suites/scoring/run/schedule contracts. | L/R |
| [Data and backups](../app-walkthrough.md#docs-data-and-backups) | Store ownership, retention/export/restore/migration procedures. | L/R |
| [Accounts and groups](../app-walkthrough.md#docs-accounts-and-groups) | Identity/session/permission/sensitivity/portability contracts. | L/R |
| [Collections and workflows](../app-walkthrough.md#docs-collections-and-workflows) | Retrieval, prompt versions and durable state sources. | L/R |
| [Deployment and operations](../app-walkthrough.md#docs-deployment-and-operations) | Active topology/source pins and documented recovery interfaces; don't run them as link checks. | L/R |

Tests: [source parsing/privacy](../tests/test_source_docs.py), [Docs browser](../tests/test_docs_browser.py), [documentation links](../tests/test_documentation.py).

## My account

| Page / manual checklist | Agent checks | Modes / limits |
|---|---|---|
| [Account](../app-walkthrough.md#my-account-account) | Identity/expiry/session/key display; isolated password/authenticator validation, revocation and other-session invalidation. | L/R; do not change the owner's password/authenticator or revoke their existing keys. |
| [Appearance](../app-walkthrough.md#my-account-appearance) | Preview/save/reset/default, account-scoped persistence, top meters, focus/contrast and table/chart/Chat rendering. | L/R using isolated account/browser preferences; H for preferred appearance. |
| [Audio](../app-walkthrough.md#my-account-audio) | Format/model/voice persistence, unavailable defaults, studio application and isolation from another user/global defaults. | L/R; restore any explicitly selected live preference experiment. |
| [Logs](../app-walkthrough.md#my-account-logs) | Known sign-in/account/key attribution, chronological sorting, paging and restricted history. | L/R; use synthetic account events for sensitive cases. |

Tests: [account/console routes](../tests/test_weightroom.py), [forms](../tests/test_forms.py), [themes](../tests/test_themes.py), [audio browser](../tests/test_soundroom_browser.py).

## Sign-in and recovery

| Page / manual checklist | Agent checks | Modes / limits |
|---|---|---|
| [Sign in](../app-walkthrough.md#sign-in-and-recovery-sign-in) | Safe deep-link/query retention through credential/authenticator steps, rejected external destinations, focus, validation and unavailable-auth guidance. | L/R; use isolated credentials for repeated failures/lockout tests. |
| [Error and refusal pages](../app-walkthrough.md#sign-in-and-recovery-error-and-refusal-pages) | Framed 403/404/500, persistent reason, request ID, recovery links and no private exception detail. | L/R; 500 injection only L. |
| [Compute or machine unavailable](../app-walkthrough.md#sign-in-and-recovery-compute-or-machine-unavailable) | Retained front-owned pages, correct stale/missing labels, node-scoped disabled controls and Wake visibility. | L/R using an already unavailable node; deliberate outage/power under M. |

Tests: [shared UI/recovery](../tests/test_ui_consistency.py), [console](../tests/test_console_browser.py), [node availability](../tests/test_node_availability_browser.py), [offline status](../tests/test_status_nodes.py).

## Connected clients and supporting routes

- [ ] **L/R:** Check aliases, fragments, SSE, JSON/media and download contracts from their [owning manual areas](../app-walkthrough.md#routes-that-are-not-separate-pages). Verify status/content type, target identity and permission refusal; don't count these as additional full pages.
- [ ] **L/R:** Validate model listing and MCP discovery against the permitted account/catalogue. Compare an existing client's version, configured base URL and error presentation without copying its credential storage.
- [ ] **W:** Make one short ordinary and streaming API reply, cancel only that request, and correlate response/model/role/request IDs. Embedding/image requests require a matching available model and their own small limits.
- [ ] **L or bounded read-only live call:** Exercise a harmless MCP tool with non-private scratch data and a denied tool/set/sensitivity. Confirm a refused tool did not execute; don't retry an unknown-outcome action blindly.
- [ ] **L:** Simulate upstream loss, slow network, auth expiry and recovery; assert no duplicate submissions or replayed partial answers. A browser-local transport failure does not prove a real server/node outage recovered.

Tests: [front Chat/API](../tests/test_front_chat.py), [front tools](../tests/test_front_toollocker.py), [tool permissions](../tests/test_tool_access.py), [gateway](../tests/test_voice_gateway.py).

## Run the maintained checks

Run from the repository root with the existing `.venv`; inspect prerequisites first. These are entry points, not evidence that every manual checkbox is automated. For CPU-only Chromium, use existing helpers that launch with `--disable-gpu`; review direct browser launches before running them and use an isolated test runner if needed. Do not install dependencies or change shared runtimes just to satisfy a docs check.

```bash
rtk proxy .venv/bin/pytest -q tests/test_voice.py tests/test_voice_gateway.py tests/test_voice_hermes.py tests/test_voice_latency.py tests/test_voice_prompt_recordings.py tests/test_voice_desktop.py
rtk proxy .venv/bin/pytest -q tests/test_voice_browser.py tests/test_voice_admin.py
rtk proxy .venv/bin/pytest -q tests/test_ui_consistency.py tests/test_console_browser.py tests/test_forms.py
rtk proxy .venv/bin/pytest -q tests/test_collection_pages.py tests/test_workflow_pages.py tests/test_soundroom_browser.py tests/test_model_image_settings_browser.py
```

Run the additional section-specific modules needed for the selected checks. Preserve failed results and distinguish a stale test selector/label/confirmation contract from a product defect by inspecting the real rendered state. If a test needs adapting, retain the original failure and exact difference; never remove a meaningful assertion or mark the product Pass merely to obtain green tests. Prior stale-contract examples and audit-only reproductions remain in the [audit record](../app-walkthrough.md#coverage-and-limits); ignored local scripts are optional aids, not portable test dependencies.

Missing Playwright/browser binaries, display support, native audio libraries, a PostgreSQL test DSN, configured models or another client are explicit blockers for those checks. Continue other sections and record precisely which assertion could not run.

## Optional maintenance

Inspect control wording and recovery guidance during R; perform effects only within the applicable M scope. Reuse the [installation/recovery guide](installation.md), [database guide](database-storage.md) and [operating constraints](agents.md#standing-permissions-and-execution-constraints).

- [ ] Record affected hosts/services, active work, benchmark pause state and immutable source boundary before a restart/deploy or configuration apply.
- [ ] For shared file cleanup, obtain usage from every configured node, protect the global usage union and refuse cleanup if any required report is missing.
- [ ] For migration/restore/identity portability, use isolated stores first; preserve the defined preview, conflict refusal, credential privacy and recovery contracts. Do not create a real identity archive automatically.
- [ ] Keep physical sleep/wake/reboot/shutdown, paid external calls and a clean reinstall outside routine execution unless specifically authorized.
- [ ] Verify actual recovery after authorized maintenance; a submitted job, successful preview or service-active flag alone is insufficient.

## Finish and report

- [ ] Stop only this run's work, release its holds/locks and restore changed settings, request acceptance and benchmark pause state. Remove only objects created by this run using their recorded IDs and the applicable deletion controls.
- [ ] Verify no owned test batch/run/download/schedule/voice session is left active. Record non-removable scratch objects and leave them disabled where supported.
- [ ] Give every page row a result through its `manual_anchor`; report mixed mode/subcase outcomes separately. List missing permissions, real-generation/device checks and maintenance checks explicitly.
- [ ] Summarize actionable findings by priority with exact steps, expected/actual behavior, source/live boundary and a receipt. Preserve the owner's results and previous acceptance records.
- [ ] Say exactly what was locally tested, live read, live exercised and owner evaluated. Do not call all functionality accepted based on route screenshots, mocks or a passing suite.

**Run result (2026-10-31): Issue.** All 96 rows have [separate local/live/action results](../data/qualification/agent-walkthrough/20261031T125005-0700/pages.json). Across 77 maintained modules, 804 cases passed and 28 were blocked. CPU-only Chromium covered 1440, 390 and 320 px, light/dark appearances and a second palette. The [run summary](../data/qualification/agent-walkthrough/20261031T125005-0700/summary.json) retains original failures, exact isolated test-contract adaptations and the unchanged screenshot retry. Maintained tests were not edited. Section regressions and route rendering do not establish every action; unmapped actions remain Not run.

**Findings / follow-up:** The nine [previous findings](../app-walkthrough.md#functionality-and-usability-audit) still reproduce; [current receipts](../data/qualification/agent-walkthrough/20261031T125005-0700/finding-recheck.json) also confirm U06. New findings U06 (Machine confirmation labels) and O01 (local worker startup) are recorded once in [Findings to act on](../app-walkthrough.md#findings-to-act-on). Passing defect reproductions record Issue for the product; no fixes were applied.

**Blocked / owner-only checks:** Authenticated live navigation was blocked by absent browser sessions, certificate trust, private Voice DNS and SSH access; remote front/gateway revisions remain unverified. PostgreSQL checks need an isolated DSN (27 cases); native desktop UI needs the missing Tk library and an isolated display (one case). Live reads were limited to access probes and local service/source/startup inspection. No live work, new owner/device evaluation or maintenance was performed; existing Voice acceptance remains unchanged.

**Cleanup:** No live objects/settings were changed. [Cleanup receipts](../data/qualification/agent-walkthrough/20261031T125005-0700/cleanup.json) verify restoration of prior test artifacts except two Voice admin screenshots overwritten by a maintained test before its fixed output paths were discovered. Unrelated repository-wide executable-bit changes appeared during the run and were left untouched; [source/content boundary](../data/qualification/agent-walkthrough/20261031T125005-0700/workspace-drift.json).
