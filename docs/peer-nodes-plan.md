# Peer compute nodes and caller-chosen destinations

Status: **implemented and Docker-qualified, 2026-11-12.** Existing computer
installations are untouched. Production rollout is outside this qualification's authorized scope.

## Problem and decisions

Node measurements previously sent pinned CPU-node tasks through an implicit default node.
That node could not resolve another node's model ID and returned “The selected local model
is not available.” The control node now owns tasks and sends each target to its named peer.

| Topic | Implemented contract |
|---|---|
| Caller destination | Optional JSON `node`, beside `model`; enabled by default. **Settings → General → Requests and queue → Allow callers to choose the node**, writable with `manage-settings`, applies immediately. |
| Setting off | A named destination receives 400 with the setting and its location; an unpinned request still uses placement. Console targets remain visible. |
| Task ownership | New tasks, batches and schedule grants always live on the control node. Old node-owned records remain on their original node; ownership is discovered without guessing a default. |
| Peers | Every node is `[machines.NAME]`; only control node is special. The former default's hostname supplies its new name automatically. |
| Model copies | Placement combines only enabled, permitted copies with the same verified weights and engine. Same names alone do not establish equivalence. |
| Placement | A copy that can start now beats a busy loaded copy. Within that group, loaded first, then estimated answer time and name. Busy copies sort by finish estimate and fewer evictions. |
| Overflow | Chat/API, batch lines and model residency calls use placement. If every awake candidate would queue or evict, wake a capable configured peer and wait within the existing bound. Work already queued stays on its node. |
| Benchmarks | An explicitly selected node remains pinned, including scheduled runs; an absent node never falls back to another measurement target. |
| Voice | **Settings → General → Speech → Preferred voice node** is a live preference. Full or unavailable preferred services fall back; each established session keeps its actual node and stream URL. Explicit caller pins obey the destination setting. |
| Admin views | Combined data identifies each node. Writes target the owning node; passive page reads never wake one. Control node accounts, settings and history remain available during node outages. |
| Backups | Each node keeps its own encrypted, verified archives. Control node schedules independent per-node results and retries only unverified nodes. |
| Compatibility | Historical request links and chained statements resolve through aliases. Protocol fields such as a peer's `compute` URL and documented old-key readers remain supported. |

## Phase 0: safety and inventory

- [x] Separate Docker project, generated accounts/keys, independent front/alpha/beta/gamma
  volumes, bounded native engines and optional read-only GPU access; no installed host paths.
- [x] Verified encrypted backups of Control node and all three peers before stored-name migration.
- [x] Unmapped requests are refused with 404; bounded diagnostics record their route without
  record IDs. There is no implicit default-node dispatch.
- [x] Audit-chain coverage reviewed: event payload/text remain byte-for-byte unchanged.
  Only attribution columns outside the chain are renamed; historical embedded names use aliases.

The original production prerequisite investigation is historical: recovery recipients were
reported ready on k-srv, jpk-main and jpk1–3 on 2026-11-12. This run did not contact those hosts,
take their backups, update them or restart their services. Fresh production backups remain a
rollout prerequisite, not evidence supplied by the Docker run.

## Phase 1: setting and task ownership

- [x] `allow_caller_to_specify_destination` defaults true and uses the existing General settings
  rows and save handlers; it is absent from System and node-specific settings.
- [x] `TaskRecords` always creates new work on the control node. Retired node stores refuse new tasks;
  non-retired stores continue serving their older history. Frozen model/node guards remain.
- [x] Old task/file/batch IDs are checked against peers without wake. A unique owner receives
  the request; collisions are explicit and unavailable owners are not silently substituted.
- [x] A target never borrows another node's model ID. Pinned runs are refused when caller
  destinations are disabled.
- [x] Rename identifiers/tables/streams: `FrontTasks` → `TaskRecords`,
  `FrontOwnedBatches` → `NoLocalBatches`, `external_tasks` → `benchmark_tasks`,
  and associated evidence/reservation tables and migration history.
- [x] Rename `front_settings` → `keep_console_settings_on_front` and
  `front_toollocker` → `toollocker_runs_on_front`, preserving old-key readers and conflict checks.
  Control node always keeps its settings; explicit false is refused with an explanation.

## Phase 2: peer configuration, names and availability

- [x] The existing `owg update` migration moves the old top-level address, Spotter address,
  key reference and wake configuration into `[machines.<hostname>]`. Control node reads its observed
  node hostname from the configured SQLite or PostgreSQL accounts store.
- [x] Validate names and collisions before changing any file; preserve the original
  `*.before-peer-nodes` file, comments, credentials and custom model paths. Repeating the
  migration changes nothing. The node stores `former_machine_name` for its old attribution.
- [x] Pairing and installers write named peers. Installer and console actions say
  “Add a compute node” for both CPU and GPU machines.
- [x] Numbered, transactional per-store migrations rename attribution, eligibility, lifecycle,
  observation and benchmark references only after a recent verified backup whose encrypted
  archive checksum still matches. Colliding observation buckets are combined, not discarded.
- [x] Conflicting aliases fail closed. Old unprefixed request IDs resolve to the renamed node;
  new requests use its normal prefix. Physical-host telemetry retains its fingerprint history.
- [x] Replace the single compute outage state with per-node notices, last-seen age and
  configured Wake actions. Control node connectivity is reported independently. Unavailable nodes'
  submissions disable while healthy-node and the control node controls remain usable.
- [x] Merge outside-service model names across peers. Control node ToolLocker and alert evidence
  have explicit routes and remain usable without a default compute node.
- [x] Voice readiness observes every reported voice service in parallel without warming it.
  Preferred/full/down routing and gateway session pinning use the actual selected service.

## Phase 3: placement and caller hints

- [x] Start-now classification excludes queues and evictions; loaded state breaks ties within
  that class instead of outranking idle capacity.
- [x] Overflow sends configured wake packets only when needed and waits within `wait` for
  the first available candidate; without wake configuration it queues on an awake peer.
- [x] Chat, completions, embeddings, image/audio requests and residency holds accept caller
  destinations and strip routing fields before forwarding. Unknown, disabled or unsuitable
  destinations receive explicit errors. Roles use the same eligible-node facts.
- [x] Residency renewals load a permitted native model through placement; release affects
  only that account's holds and does not wake peers. Hosted models are never accidentally
  invoked by residency operations.

## Phase 4: combined administration

- [x] Status, Models, Files, Fingerprints, alert evidence and telemetry expose answering peers
  and identify missing ones. Passive reads carry no-wake policy through internal lookups.
- [x] Requests, Usage and AI log have shared Node filters; request/event rows identify their
  origin. System logs require the selected node. Model/file/restore writes retain their node.
- [x] Preserve the established control node event stream for request/event pagination. Nodes already
  forward it durably to the control node, so one ordered cursor is authoritative; introducing separate
  per-node cursors would duplicate this history and undermine stable paging.
- [x] Shared filters, notices, recovery inventories and existing component templates are reused
  in pages/fragments. Hidden-state handling, narrow layouts and dynamic replacements were reviewed.

## Phase 5: independent backups

- [x] Control node and each compute node use their own backup routes, encryption, retention and
  restore previews; a failed peer never substitutes another node's archive.
- [x] A backup schedule accepts an explicit node list; empty means Control node and all configured
  peers. Saved unavailable choices remain visible instead of silently broadening scope.
- [x] Each occurrence/node has a deterministic backup ID and durable receipt. Verified peers
  are retained during outages; retries create only the missing archive. Missed results remain visible.
- [x] PostgreSQL recovery uses client tools matching the server's major version and verifies
  a real restore to a temporary database. Migration registration includes media and installation
  tables, so engine transfers recognize the complete accounts store.

## Qualification

Run against [the disposable cluster](installation.md#docker-qualification-with-real-models),
using Chromium interactions rather than pytest suites. Screenshots and JSON are generated
outside the checkout by `tests/docker/qualification/browser.py` and `scenarios.py`.

| Contract | Observed result |
|---|---|
| Navigation | 60 live routes at 1440 and 390 px: complete SQLite and PostgreSQL crawls each passed 120 visits, all 200, no browser errors, failed requests or document overflow. |
| Install and inference | Hugging Face download; one-node CPU install and three-node installation; real CPU and NVIDIA llama.cpp replies. |
| Destination policy | Alpha/beta/gamma pins answered on their node; unknown node 404; off switch refused pins with its General-settings explanation while unpinned inference answered; pinned role and embedding calls succeeded. |
| Busy placement | Beta generated 1,800 tokens while a new unpinned request loaded and answered on idle alpha. |
| Wake overflow | Passive Status/Models/Usage/Backups reads sent no wake. With beta busy and gamma stopped, a real 102-byte magic packet reached the isolated receiver; starting only the disposable gamma container let the waiting request answer successfully. |
| Benchmarks | Real load measurements completed nine samples on each of alpha, beta and gamma. An imported two-case quality benchmark executed both cases on all three, each scoring 50%: strict JSON passed, exact text failed. This exposed and verified fixes for output budgets below 32 tokens and retained GPU-process reuse between cases. |
| Images | Complete SDXL Turbo GGUF installed on beta and gamma; real 512×512 generation persisted, decoded and downloaded as PNG. |
| Speech and retrieval | Kokoro produced a playable, downloaded 3.15 s WAV; Moonshine transcribed it correctly. A local embedder indexed a source into 768 dimensions and retrieved the matching excerpt. |
| Workflows | Qwen2.5 3B completed research, acceptance, drafting and final acceptance using the indexed source and built-in prompts. Smaller models' malformed citations and abstentions were refused; accepted research survived failed drafts and retries. Private prompt version creation was exercised. |
| Tools and accounts | Browser MCP connection listed two tools, listed/read its shared sample, and refused an outside-folder path. Restricted account permissions, password sign-in and immediate key revocation were exercised. |
| Voice | Real streaming recognition and spoken model reply; simultaneous calls used preferred alpha and fallback beta; stopping alpha's voice service reached Listening on beta and completed a spoken turn. |
| Backups | Four independently verified archives and correct node-specific restore previews. Scheduled gamma outage retained the other three receipts, then completed gamma after it returned. The qualification schedule was paused afterward, retaining its history. |
| Migration | SQLite and PostgreSQL attribution migrations kept valid audit chains. A verified older backup transferred 49 tables to PostgreSQL, then renamed old attribution at startup; restart was idempotent. Retention preserves renamed historical request text while removing orphan text. |
| PostgreSQL recovery | Browser SQL export contained all ten selected stores; encrypted PostgreSQL backup passed a real temporary-database restore after matching-client selection was fixed. |
| Update configuration | Existing migration helper ran inside containers: front/node hostname rename, original recovery copies, unchanged rerun, collision refusal and preserved custom library paths. |
| Critic | Independent desktop/mobile reviews covered populated, empty and outage states; final reviews approved Audio/Speech/Voice, completed workflows and expanded benchmark evidence, including question visibility and subsecond timing precision. |

The source component audit and syntax checks supplement these browser checks. Unit suites are
not used as a substitute for browser qualification. Actual physical-host power-on, production SSH rollout,
external telephone delivery, and every third-party model family require their own environment;
Docker results do not establish those outcomes.

## Production rollout and rollback

The owner requested that existing installs stay untouched. The following remains an operating
procedure for a separately authorized rollout, not an action performed by this task:

1. Obtain and verify fresh encrypted backups on the control node and every node, including settings and
   each node's stores. Keep recovery identities outside the machines being updated.
2. Push the reviewed commit. Update control node and all nodes together through their existing
   configured branch with `owg update; owg restart`; do not introduce pins, alternate runtime
   trees, `PYTHONPATH` overrides, service overrides or wrapper launchers.
3. Verify the effective running source and live behavior on every host, including General's
   destination setting, each pinned load benchmark, busy-node overflow, old links and a peer outage.
4. To roll back, restore the original `*.before-peer-nodes` settings and matching verified
   pre-migration database backups, then use the established OWG route for the previous code.
   Do not run old code against renamed stores without restoring the matching data.
