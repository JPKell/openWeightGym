# Agent status and backlog

This is the approval/status ledger, separated from the human guide. States below preserve the
last recorded evidence through **2026-10-30**. Current behavior belongs in the
[scoped guides](agents.md#documentation-maintenance).
Earlier milestone descriptions/protocols may be superseded by later entries.

## Milestones

Preserve identifiers; the next new milestone is **165**. These are historical approved increments,
not a queue to execute again. Detailed interview narratives are recoverable from Git.

| ID | Scope | Recorded state / evidence |
|---|---|---|
| 1 | Model API forwarding | done |
| 2 | On-demand model loading | done |
| 3 | Queue and wait limits | done |
| 4 | Exclusive locks and priority | done |
| 5 | Multiple GPUs | done |
| 6 | Users and groups | done |
| 7 | Event log | done |
| 8 | Sensitive capture and audit chain | done |
| 9 | ToolLocker MCP gateway | done |
| 10 | Hosted offload | done |
| 11 | Live model catalog | done |
| 12 | Expiring sign-in sessions | done |
| 13 | Front web console | done |
| 14 | Model management pages | done |
| 15 | Model file management | done |
| 16 | Status and request controls | done |
| 17 | Spotter telemetry and services | done |
| 18 | Draining, power and front routing | done |
| 19 | Account overrides and app sessions | done |
| 20 | User/group pages | done |
| 21 | Request capture pages | done |
| 22 | Authorized no-log requests | done |
| 23 | Failure diagnostics | done |
| 24 | Web chat | done |
| 25 | Hugging Face downloads | done |
| 26 | Measured GPU sharing | done |
| 27 | Adapters and projectors | done |
| 28 | Hosted provider registration | done |
| 29 | Chat context summaries | done |
| 30 | Chat attachments | done |
| 31 | Two-model chat comparison | done |
| 32 | Web settings | done |
| 33 | Settings/data directory layout | done |
| 34 | Model fingerprints | done |
| 35 | Durable batches and recovery | done |
| 36 | Category and benchmark navigation | done |
| 37 | Durable resource benchmarks | done; deployed and real GPU runs verified |
| 38 | Standalone benchmark hosting | done; standalone hosting superseded by 39 |
| 39 | Benchmarks inside WeightRoom | done; deployed and real GPU restart recovery verified |
| 40 | Named model roles | done |
| 41 | Deterministic quality suites | done |
| 42 | Usage reports | done; deployed, API and browser verified |
| 43 | Versioned grading and rescoring | Deployed |
| 44 | Account rename and case-insensitive login | deployed 2026-10-13 |
| 45 | Dry fit | deployed 2026-10-13 |
| 46 | GPU discovery and automatic splitting | deployed 2026-10-13 |
| 47 | Parallel model controls | deployed 2026-10-13 |
| 48 | Front-owned web chat | done |
| 49 | Model sort order | deployed 2026-10-13; multi-node/table correction 2026-10-19 |
| 50 | External model plugins | framework deployed in WeightRoom and turnstile |
| 51 | Historical benchmark visibility | done |
| 52 | Stable model IDs | done |
| 53 | Role/benchmark explanations | 37 relevant checks passed |
| 54 | Account themes | deployed without restarting WeightRoom |
| 55 | Benchmark UI refinements | deployed on 2026-10-12 |
| 56 | WeightRoom/ToolLocker rename | deployed on 2026-10-12 after Claude finished |
| 57 | Persistent model locks | implemented 2026-10-12 and deployed 2026-10-13 |
| 58 | Benchmark-informed roles (P1) | deployed 2026-10-13 |
| 59 | Paired backup/restore (P2) | deployed 2026-10-13 |
| 60 | Generated extraction cases | deployed 2026-10-13 |
| 61 | Reliability and feedback (P3) | deployed 2026-10-13 |
| 62 | Database inventory and exports | deployed 2026-10-13 |
| 63 | Shared database boundary (DB P1) | deployed 2026-10-13 |
| 64 | Portable schemas (DB P2) | deployed 2026-10-13 |
| 65 | Native recovery drivers (DB P3) | deployed 2026-10-13 |
| 66 | Suite installer | deployed and compatibility-qualified 2026-10-16; SQLite-only selection and PostgreSQL 18 native-client audit passed; [installation guide](installation.md) |
| 67 | Offline engine migration (DB P4) | deployed 2026-10-16; all 13 current stores passed isolated PostgreSQL 18 round trips/native restores, with separate rollback fixtures; live bindings remain SQLite; [evidence](database-storage.md#p4---offline-engine-migration) |
| 68 | Store health (DB P5) | deployed and live-verified 2026-10-16; primary SQLite health/paired-backup age and isolated PostgreSQL 18 pressure/TLS/read-only signals passed; [scope](database-storage.md#p5---operational-postgresql-support) |
| 69 | Mock tool-use/recovery suites (P4) | completion confirmed by the owner 2026-10-14 |
| 70 | Operational alerts (P5) | completed and accepted by the owner 2026-10-14 |
| 71 | Recurring schedules (P6) | owner confirmed both installations updated, schedule editing working and acceptance passed |
| 72 | Remote MCP servers | deployed and live-qualified 2026-10-16; [remote-host evidence](installation.md#remote-tool-hosts) |
| 73 | Tool permissions and remote hosts | deployed and live-qualified 2026-10-16 |
| 74 | Tool folders and host installer | deployed and live-qualified 2026-10-16 |
| 75 | Relay renamed to Turnstile | deployed; live CLI/service/config confirmed 2026-10-16 |
| 76 | Front account authority | deployed; live front account/Turnstile checks and focused tests confirmed 2026-10-16 |
| 77 | Machine-key rotation and emergency mode | deployed by 2026-10-15 |
| 78 | Front-owned voice model calls | folded into 76; deployed and owner Direct voice verified by 2026-10-16 |
| 79 | Retained arithmetic audit | approved and completed 2026-10-14 |
| 80 | Arithmetic-v3 | deployed; retained score-range qualification complete, [A2 reconciliation](benchmarks/qualification-2026-10-15.md#four-model-spread-sweep--retained-a2-reconciliation-2026-10-16) |
| 81 | Dedicated Reliability page | deployed and live route-checked 2026-10-16 |
| 82 | Demand-driven model reuse | deployed; live setting and focused checks 2026-10-16 |
| 83 | Front roles/evidence/reliability | deployed by 2026-10-15 |
| 84 | Selection-safe automatic refresh | deployed to front 2026-10-14 |
| 85 | Local artifact cleanup | completed locally 2026-10-14 |
| 86 | Retrieval hardening | deployed; representative protocol-5 qualification complete 2026-10-18; [results](benchmarks/qualification-2026-10-15.md#representative-results-and-limits--2026-10-17) |
| 87 | Embedder resource benchmarks | deployed; live-qualified 2026-10-16 ([benchmark contract](../benchmarks/README.md)) |
| 88 | Scoped Home summaries | deployed and live route-checked 2026-10-16 |
| 89 | Voice download and administration | deployed; H2 page/playback qualification owner-accepted 2026-10-16 |
| 90 | Chat sidebar and options | deployed and live route-checked 2026-10-16; sidebar and plugin-control scope updated locally 2026-10-16 |
| 91 | Front event/text history | deployed by 2026-10-15 |
| 92 | Request diagnostics | owner-confirmed complete 2026-10-15; matching front/compute code and focused tests confirmed 2026-10-16 |
| 93 | Reviewed model configuration/presets | deployed; bounded live protection, apply/undo, lease and shared-quant qualification passed 2026-10-16; [contract](loadcoach.md#validated-model-setup-and-presets-c05-milestone-93) |
| 94 | Versioned case imports/regressions | deployed; imported case execution verified on the retained A2 sample, broader spread not implied |
| 95 | CLI model ordering | deployed; live CLI and focused checks 2026-10-16 |
| 96 | Optional wait admission | deployed; limit confirmed off and focused checks 2026-10-16 |
| 97 | Saved Dry fit load-outs | deployed; bounded live protection, apply/undo, lease and shared-quant qualification passed 2026-10-16; [contract](loadcoach.md#validated-model-setup-and-presets-c05-milestone-93) |
| 98 | Abstention suite | deployed; retained A2 results reconciled 2026-10-16 |
| 99 | Table reasoning suite | tables-v6 / protocol 8 deployed and representative qualification complete 2026-10-18; [results](benchmarks/qualification-2026-10-15.md#representative-results-and-limits--2026-10-17) |
| 100 | Voice latency diagnostics | deployed; real desktop/browser timings observed and revised latency gate owner-accepted 2026-10-16; V7 capacity remains separate |
| 101 | Native conversation evaluations | deployed and live-qualified 2026-10-18; [native format and rejection evidence](benchmarks/qualification-2026-10-15.md#representative-results-and-limits--2026-10-17) |
| 102 | Multi-turn consistency suite | deployed; current representative qualification complete under milestone 121 |
| 103 | Paired free/schema JSON | protocol 6 deployed and D7 representative qualification passed 2026-10-17; [audit](benchmarks/qualification-2026-10-15.md#representative-results-and-limits--2026-10-17) |
| 104 | Expanded mock tool-use cases | representative protocol-3 qualification cleared 2026-10-18; [results and coverage](benchmarks/qualification-2026-10-15.md#representative-results-and-limits--2026-10-17) |
| 105 | Private collections | deployed; owner accepted bounded H3 source/citation and missing-information checks 2026-10-16; selected-page layout deployed 2026-10-18; [receipts](collections.md#owner-grounding-review--2026-10-16) |
| 106 | Versioned prompts and workflows | deployed; H3 research/agent acceptance and draft generation verified live 2026-10-16; owner accepted draft/grounding and the workflow is completed; selected-page layout deployed 2026-10-18 |
| 107 | Advisory spending allowances | deployed and live-qualified 2026-10-17; [soft-policy workflow, attribution and coverage limits](allowances.md) |
| 108 | Multi-step reasoning suite | deployed; current reasoning-v2 qualified under milestone 119 |
| 109 | Instruction hierarchy suite | deployed; representative hierarchy-v2 qualification complete under milestone 120 |
| 110 | Shared presets across quantizations | deployed; bounded live protection, apply/undo, lease and shared-quant qualification passed 2026-10-16; [contract](loadcoach.md#validated-model-setup-and-presets-c05-milestone-93) |
| 111 | Turnstile in-flight limit | implemented and live-checked 2026-10-15: a 260-request concurrent check yielded 201 answers and 59 immediate busy refusals |
| 112 | Named machine inventory | deployed 2026-10-15 |
| 113 | Shared weights identity | deployed 2026-10-15 |
| 114 | Front hosted inference | implemented and included in the owner's first 2026-10-15 deployment |
| 115 | Front-owned batches | deployed on front |
| 116 | Multi-node placement | deployed and live-qualified 2026-10-16 on a same-host, one-card-each rig: signed inventory and matching hashes, warm placement, failover in both directions, distinct request records, rig-owned active Status/Peek/Diagnostics/Stop, temporary `:fast` role selection, no midstream replay, and a five-line batch completed across the switch (three rig, two primary) |
| 117 | Admin system Docs | deployed and live route-checked 2026-10-16 |
| 118 | Bounded report queries/cache | deployed and live-qualified 2026-10-16; page-load follow-up deployed and Chrome-checked 2026-10-25 ([contract and measurements](loadcoach.md#turnstile-report-hardening-milestone-118)) |
| 119 | Reasoning-v2 | protocol 3 deployed; representative qualification passed 2026-10-18; [audit](benchmarks/qualification-2026-10-15.md#representative-results-and-limits--2026-10-17) |
| 120 | Hierarchy-v2 | deployed and D7 representative qualification complete 2026-10-17; [results](benchmarks/qualification-2026-10-15.md#representative-results-and-limits--2026-10-17) |
| 121 | Turns-v5 | protocol 6 deployed; representative native qualification complete 2026-10-18; [results](benchmarks/qualification-2026-10-15.md#representative-results-and-limits--2026-10-17) |
| 122 | Benchmark server reuse policy | deployed; live-qualified 2026-10-16; dead-probe reservation/reuse corrections deployed and endpoint-verified 2026-10-17 ([cleanup](loadcoach.md#general-conversation-evaluations-benchmark-part-4-milestone-101)) |
| 123 | Physical-host telemetry | deployed and live verified on compute, front and VPS 2026-10-16; state colors and green-to-orange utilization fills deployed and live-render verified 2026-10-16 |
| 124 | Front ownership/compute retirement (DB1) | completed and live-verified 2026-10-16; 17 stale tables retired; [ownership and recovery](installation.md#db1-ownership-and-retirement-milestone-124) |
| 125 | Measured CPU RAM admission | completed and live-qualified on three workers 2026-10-16; isolated two-hour remote trial passed 2026-10-17 ([CPU evidence](cpu-embedding-hosts.md)) |
| 126 | Per-host settings/authorization | completed and live-qualified on all three hosts 2026-10-16; original sleep policies retained |
| 127 | Automatic collection indexing status | deployed on front and live-verified 2026-10-16: indexing changed to ready automatically without a page refresh; focused browser/collection checks passed |
| 128 | Front ToolLocker and telemetry backfill | deployed and live-verified 2026-10-16; [ownership and recovery](database-storage.md#front-toollocker-and-remaining-telemetry-milestone-128) |
| 129 | Front pages without LoadCoach | deployed 2026-10-16; front page routes and Hermes readiness live-checked, offline route contract locally passed without repeating a live LoadCoach shutdown |
| 130 | Logical models across locations | deployed; three-node resource qualification passed 2026-10-17; model-page section order deployed 2026-10-18; [receipts and limits](cpu-embedding-hosts.md#native-cpu-resource-benchmarks-g18) |
| 131 | Node-specific Dry fit/RAM policy | deployed and live-verified on the GPU node and both CPU nodes 2026-10-17 |
| 132 | Front cache and verified distribution | deployed 2026-10-17; three-node storage and distribution controls live-verified, receiver/atomic-install contract tested locally; live large-file transfer intentionally not started |
| 133 | Per-node model root | deployed 2026-10-17; initial compute-to-front copy verified after transfer |
| 134 | Cache/storage separation | deployed 2026-10-17; initial library inventory complete; [effective storage settings and verification limits](loadcoach.md#the-files-pages) |
| 135 | Queue outstanding benchmarks | deployed and live-qualified 2026-10-17; [fresh-result queuing and local stale-evidence checks](cpu-embedding-hosts.md#native-cpu-resource-benchmarks-g18) |
| 136 | Compute nodes administration | front controls deployed and live route-verified 2026-10-18; omitted-GPU startup default activated on compute with both cards detected and private configuration preserved; [worker evidence](loadcoach.md#the-command-line) |
| 137 | Sampling/reasoning controls | deployed and live route-verified 2026-10-18 |
| 138 | Conditional RAM fit display | deployed and live route-verified 2026-10-18 |
| 139 | CPU/GPU placement controls | deployed and live route-verified 2026-10-18 |
| 140 | Shared action alerts/role layout | deployed and live route-verified 2026-10-18 |
| 141 | Per-node Files/cache/storage managers | deployed on front and authenticated page markers verified 2026-10-18 |
| 142 | Cloud APIs route/label | deployed on front and authenticated page markers verified 2026-10-18 |
| 143 | Per-node resource benchmark layout | deployed on front and authenticated page markers verified 2026-10-18 |
| 144 | Reliability issues-only filter | deployed on front and authenticated page markers verified 2026-10-18 |
| 145 | Maintenance CLI/chart controls | deployed 2026-10-18; front page markers and CLI help/read-only lists on all workers verified; mutating CLI actions covered by isolated fixtures; [evidence](loadcoach.md#the-command-line) |
| 146 | Resource score heatmaps | deployed and live Chromium-verified 2026-10-18; receipt: `data/qualification/gates-2026-10-16/ui146-deployment-20261018.json` (absent from this checkout) |
| 147 | Inference engine registry | deployed 2026-10-18; [engine boundary](loadcoach.md#inference-engine-boundary); existing native binaries/libraries unchanged |
| 148 | Background model-file maintenance | deployed 2026-10-19 from an immutable release after an empty benchmark-queue check; front and all three LoadCoach nodes active and source/route checked |
| 149 | Application sweep/source Docs | deployed and live-verified on all three computers 2026-10-20; [sectioned changes, checks and delivery](application-sweep-2026-10-20.md) |
| 150 | Request filters and client access | deployed and live-verified on all three computers 2026-10-20; [request filters and access contract](loadcoach.md#request-filters-and-client-access-milestone-150) |
| 151 | Observability and traceability | implemented, deployed and live-verified 2026-10-20; [changes and acceptance](observability-work-plan.md#implemented-changes-by-code-section) |
| 152 | Encrypted identity portability | installed on all three computers and front guidance live-verified 2026-10-20; [coverage and commands](installation.md#identity-and-credential-portability-milestone-152) |
| 153 | Model catalogue, verified node installation and per-node settings | deployed 2026-10-26; live file inventories/pickers verified on three nodes; [deployment and limits](installation.md#runtime-source-overrides) |
| 154 | File-based audio models and Kokoro | deployed 2026-10-26; runtime family/file selection and CPU speech live-verified; [coverage](soundroom-plan.md#execution-status) |
| 155 | Cloud API key management | deployed 2026-10-26; field/key-presence live-verified; private writes covered by API/browser fixtures; [contract](loadcoach.md#34-cloud-apis-registered-from-the-site) |
| 156 | Benchmark pages, model-type node measurements including embedders, and language-only score/run lists | deployed and live Chromium-verified 2026-10-26; [page contract](../benchmarks/README.md#pages-scores-and-result-identity) |
| 157 | API compatibility and request-header reference in Docs | deployed and live Chromium-verified 2026-10-26; [release and recovery](installation.md#runtime-source-overrides) |
| 158 | Model/image forms, shared encoders, SDXL, Dry fit, Settings navigation and node-aware single-pair schedules | implemented, tested, deployed and live-verified 2026-10-27; [contracts and release receipts](model-image-settings-plan.md) |
| 159 | Node wake controls, offline pages and retained inference catalogue | deployed 2026-10-30 to front and all nodes; [operation](README.md#where-things-run) |
| 160 | Module readability/efficiency/adaptability audit and llama.cpp reasoning discovery | deployed 2026-10-30 to front/all nodes; [audit, validation and limits](application-sweep-2026-10-20.md#module-audit-and-reasoning-discovery-started-2026-10-29) |
| 161 | Console model/settings polish (M0–M5) | deployed 2026-10-30 on front and all workers; [release, live checks and rollback](installation.md#console-polish-release-2026-10-30) |
| 162 | llama.cpp runtime refresh | active and endpoint-verified on compute/front_cpu/remote_cpu 2026-10-30 at `344846b4f`; [installation and recovery](installation.md); no GPU inference qualification |
| 163 | Shared console components and UI consistency (U0–U7) | deployed 2026-10-30 on front WeightRoom/Voice gateway; [release, live checks and rollback](installation.md#ui-consistency-release-2026-10-30) |
| 164 | Console usability (V1–V16) | implemented and deployed; [qualification and walkthrough limits](../makeBetter.md#execution) |

## Voice acceptance

Owner authorized V1–V10; physical acceptance is owner confirmation, separate from automated tests.

| ID | Scope | Recorded state / evidence |
|---|---|---|
| V1 | Expiring model holds | implemented and tested |
| V2 | Direct CPU speech/browser | implemented; generated audio and Chromium verified |
| V3 | Interruption and playback history | implemented; browser and telephone simulations verified |
| V4 | Gateway/readiness/private browser | deployed; simulated sleep/wake verified; Tailscale Serve HTTPS configuration observed live and private HTTPS/microphone qualification confirmed complete by the owner 2026-10-14 |
| V5 | Signed telephone route | deployed; public HTTPS/WSS simulation verified; real telephone qualification confirmed complete by the owner 2026-10-14 |
| V6 | Isolated Hermes API | deployed; real search verified; voice approval UI not implemented |
| V7 | Two-call capacity | bounded desktop/Safari two-call qualification completed 2026-10-16: isolation, overlap, mute fix, resources, reconnect and cleanup passed; this installation admits two calls; [evidence and limits](voice.md#h1-two-source-trial--2026-10-16) |
| V8 | Native desktop client | implemented; Linux GUI/audio simulation verified; native desktop execution/audio qualification confirmed complete by the owner 2026-10-14 |
| V9 | Echo cancellation | implemented; generated feedback and simultaneous speech tested; physical-device qualification confirmed complete by the owner 2026-10-14 |
| V10 | Browser shared identity | deployed and [bounded G13 identity qualification complete](voice.md#v10-browser-identity-qualification--2026-10-16) 2026-10-16; owner phone Direct voice passed |

## Open delivery and qualification

- **Mirror Wall image generation:** [combined deployment and validation](image-generation-plan.md#combined-deployment-2026-10-24) complete; [owner visual quality review and native klein 4B qualification](image-generation-plan.md#execution-status) remain; SDXL qualification is recorded under milestone 158.
- **SoundRoom audio:** [deployment and qualification](soundroom-plan.md#execution-status) complete through the 2026-10-25 repeated-clone fix; 21 qualified profiles live; required voice-service permissions restored ([call settings](../voice/README.md#browser-call-settings)). Open: spoken chat untried by a person.
- **Installer acceptance:** owner plans a clean reinstall near application completion (roughly
  95%). Follow the [acceptance contract](application-sweep-2026-10-20.md#clean-install-acceptance)
  when separately scheduled; this is not authorization to uninstall now.
- No gate remains from the earlier work-plan loop: cleared-gate receipt `data/qualification/gates-2026-10-16/final-cleared-gate-acceptance-audit-20261018.json` (absent from this checkout).
- Bounded qualifications do not establish full-catalogue performance, a second physical GPU host,
  a live PostgreSQL cutover or unlimited voice capacity. Retain the owning guide's limits.

## Proposals

Unapproved candidates: reproduce a current need, select a bounded increment and obtain scope
before implementation. Remove completed work instead of maintaining a second completion log.
Historical research remains in Git (`05c0a779:competitor.md`); it is not replacement qualification.

| ID | Candidate / condition |
|---|---|
| C11 | Qualify a second physical GPU host after the owner supplies/authorizes it: network loss, ownership, recovery, resources, streaming/cancellation/auth/accounting. Rental spend needs separate scope. |
| C12 | Organization identity/deployment only for a concrete need: OIDC group mapping, API-key/recovery-admin access, upgrade/restore/rollback and offline install; preserve SQLite-only operation. |

### Conditional maintenance candidates

| Area | Candidate, only if reproduced/selected |
|---|---|
| Scheduling/admission | Per-card controls, fairness, compatible spare capacity, bounded stuck work/drains and fresher memory sampling; preserve active work/holds. |
| Request observation | Expand bounded live peek/retained output only when current limits obstruct diagnosis. |
| Audit/capture | Independent chain anchoring; bounded sensitive buffering; agreed encryption/retention; deduplication and failed-final-write handling without claiming delivery was undone. |
| Keys/sessions | Last-use/expiry, durable guessing/replay limits, session revocation/lifetime, password rules, authenticator QR/recovery codes. |
| Store concurrency | Offload measured DB/password stalls and detect concurrent model-edit conflicts. |
| Tool access/discovery | Schema-change approval, private environment references, permission-scoped find/call discovery when catalog size warrants it. Shell execution needs an isolation contract; web tools use approved external packages. |
| Tool execution | Safe concurrency, bounded streaming, client batch compatibility, cancellation and known-outcome retries; visible startup state and bounded authenticated web-chat calls. |
| Model files | Recursive/orphan discovery, cancellable atomic cross-drive moves, multipart/projector association, descriptions/manual overrides and reload-now; trash requires revisiting permanent deletion. |
| Downloads | Pagination/branches, collision-safe paths, shared-drive reservations, cancellable hashing, failure/rate reporting and optional model creation. |
| Measurements/fingerprints | Unlike-device measurements, CLI calibration, explicit calibration progress, optional pre-admission hashing and agreed same-size/same-date detection costs. |
| Image/audio benchmarks | Very low priority by owner decision 2026-10-26; consider task-specific evaluations only if selected later. |
| Adapters/projectors | Manual association when metadata fails, clearer shared-server ownership, consistent missing-projector errors; qualify concurrency/CUDA-graphs changes. External training requires scope. |
| Batches | Interrupted-state clarity, bounded parallelism/window, effect-aware retries, blocked-work wake policy and optional P5 completion notices. |
| Providers | Required native formats, endpoint/key preflight, hosted benchmark provenance, CLI management, pricing and hard reservations. |
| Roles | Explicit hosted capabilities, two-role comparison, CLI management; explainable capability/context-first auto choice and bounded busy-role policies without another scheduler. |
| Console | Partial outage results, accessible chart equivalents and fuller retained reasoning/output when justified. |
| Accounts/groups | Rename/copy groups, model-reference validation and clearer effective abilities; page-access audit requires a privacy/retention decision. |
| Diagnostics | Configurable failure retention, better service/time context, prompt limits, GPU failure snapshots and journal-wide search. |
| Chat | Office/OCR, folders/tags/export, syntax/formulas/diagrams, branching and hosted reasoning controls; preserve selection/streaming and bound temporary resources/readiness. |
| Settings | Field-level safety, front restart controls, reset propagation/follower delay and unavailable/prior values. |
| Spotter/power | Activity/burst detection, drain/action accuracy, service/polkit recovery, peaks/other GPUs and private HTTPS; physical transitions require renewed authorization. |
| Turnstile | Service-down waits, cancellation during wake waits, private HTTPS and interruption-aware deployment. |
| No-log | Visible safe controls; anonymous usage requires explicit policy change and must preserve no retained evidence. |
| App sessions | App-specific concurrency/priority/budget without bypassing user restrictions or guessing limits. |
| Plugin portability | Native Windows/macOS installers only if needed; qualify reasoning events only where the adapter exposes them. |
| C04 case imports | Label standard-task subsets; keep imports data-only and historical versions immutable. |
| C05 model setup | Additional coexistence/eviction cost controls require measured avoidable reloads; never override admission, active work or holds. |
| C07 allowances | Hard ceilings require defined retry/cancellation/missing-usage/reservation/app-budget policies; current allowances remain soft. |
| C08 collections | Sharing, OCR, embedder prefixes and larger vector stores need selected scope; preserve caller-authenticated inference. |
| C10 workflows | General editing, external research/tool effects, shared prompts and model rebinding need selected scope; keep native-agent sessions independent. |

### Optional voice extensions

- **C06:** recognition alternatives, speech chunking or stricter latency against the
  [accepted baseline](voice.md#h1-owner-latency-acceptance--2026-10-16); retain generated/device checks.
- Approval UI before broader Hermes tools; approval-required runs currently stop.
- Optional telephone mode selection. In-session changes must cancel the old owner, clear playback,
  close its history and start/announce a new permitted session; never silently mix modes/history.
- WebRTC network transport only for a concrete need; existing local echo cancellation is not
  network transport. Preserve bounded queues, ordering, cancellation and signed/authenticated ingress.

Record new approvals here before execution; retain existing P/C/Part numbers and never reuse
completed identifiers. Current contracts and recovery stay in their owning guides; detailed logs
and row-level evidence stay in machine-readable artifacts.
