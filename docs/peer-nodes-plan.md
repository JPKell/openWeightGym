# Peer compute nodes and caller-chosen destinations

Status: **plan for review, 2026-11-12.** Nothing here is implemented. The decisions come from the
owner's interviews on 2026-11-12, including the three review points (see *Resolved review points*).

## Why

Benchmark runs of `wl9-smollm-135m-import` on the three CPU nodes (jpk1–3) failed with
"The selected local model is not available." All three nodes have the file and a model entry.
The run never reached them:

1. Node measurements posts one task per target, each with that node's model ID and
   `machine: jpk1`, `jpk2` or `jpk3` ([benchmarks/app.py](../benchmarks/app.py) `queue_runs`).
2. The front has `[turnstile] front_tasks` unset (false). `FrontTasks.handle` then hands
   `POST /loadcoach/tasks` back to the generic pass-through. That sends it to the default node,
   "compute" (jpk-main), and ignores `machine` ([turnstile/app.py](../turnstile/app.py) `pass_on`).
3. jpk-main can't resolve jpk1's model ID, so it answers 404. Its log shows one `201` (its own
   copy) and three `404`s. jpk1–3 never received a request.

The setting name hides what it does. "Front-owned tasks" covers three behaviours: where task
records are stored, whether a caller may choose the node, and (on compute) whether some program
settings belong to the front. The deeper problem is that one node, "compute", acts as the
default for the whole cluster. About 220 code references, the console's single
"compute up/down" state, backups, request-ID numbering and the pass-through all assume it.

## Decisions

| Topic | Decision |
|---|---|
| Setting | `allow_caller_to_specify_destination`, default **yes**, in the top-nav **Settings** tab under **General** (not System, which only shows the running system). Changed by accounts with `manage-settings`. Takes effect at once; no restart. |
| Meaning | Only whether a caller may choose the node. Task and batch records always live on the front, with no switch. |
| Off | Placement picks the node. A request that names a node gets a clear error naming the setting and where it is. Targets stay visible in the console. |
| API | Optional `"node": "jpk1"` field in the JSON body, next to `model`. |
| Existing installs | The setting is on after update. Old task history stays where it is and remains readable. No history import. |
| Nodes | Every node is a peer. "compute" becomes `[machines.jpk-main]`, named automatically from the node's hostname. Stored references are renamed after a verified backup. Only the front is special. |
| Names | The rename covers everything: user-facing text, code identifiers, database tables and migration stream keys. |
| Voice | A preferred voice node, chosen in Settings → General, takes sessions. Other nodes running voice take them only when it is full or down. |
| Busy | A node is busy for a request if the request would wait in a queue or would unload another model. |
| Capable | Same verified weights (SHA-256) and engine, entry enabled at that node, model allowed for the caller's group. This is today's rule for treating copies as one model. |
| Overflow | Chat/API, batch lines and admin calls. If a capable node can start now, it wins. If no awake node can start, wake a capable node that has wake settings. |
| Benchmarks | A run that names a node always runs there; it measures that node. |
| Admin views | One list across all nodes, with a Node column and filter. Writes go to the row's node. Each node keeps its own files, data and backups. |
| Offline nodes | Views show the answering nodes, a notice for each missing node ("jpk2 not answering, last seen 4 min ago") and a **Wake jpk2** button. Views never wake a node on their own. |
| Backups | Each node backs up its own data. The front schedules them and shows every node's status. |
| Delivery | The rename and the routing change ship together, built in the phases below with tests. |
| Rollout | After the owner's go-ahead: `owg update; owg restart` on the front and each node over SSH, then live verification. |

## Current state (2026-11-12)

| Machine | Address | Code | `machine_name` |
|---|---|---|---|
| Front (k-srv) | 10.77.10.42 | `4f29349` | — |
| jpk-main (GPU, default "compute") | 10.77.10.84 | `629f3ab` (14 behind) | `compute` |
| jpk1 (CPU) | 10.77.10.11 | `629f3ab` | `jpk1` |
| jpk2 (CPU) | 10.77.10.12 | `629f3ab` | `jpk2` |
| jpk3 (CPU) | 10.77.10.13 | `629f3ab` | `jpk3` |

The front sets `[turnstile] compute = "http://10.77.10.84:8442"` and `front_settings = true`.
It leaves `front_tasks` unset. No node has wake settings, so the wake rules below can't fire
until a `wake_mac` is added.

### What still depends on the default node

| Area | Where | Today |
|---|---|---|
| Unrouted requests | `pass_on` → `through(...)` with no `machine` | Any path without its own handler goes to "compute". |
| Task creation (legacy) | `FrontTasks.handle`, `front_tasks = false` | Goes to "compute" whatever `machine` says. |
| Console availability | `weightroom/app.py` `ask` / `noted` / `ComputeOff` | One up/down flag, driven by answers from the default node. |
| Request IDs | `loadcoach/log.py` `public_number`, `origin_number` | "compute" keeps unprefixed numbers; other nodes get a 12-bit name prefix. |
| Backups | `turnstile/tasks.py` schedule `backup`, `weightroom/app.py` Backups page | Always `machine='compute'`. |
| Wake | `[turnstile] wake_mac` | Only the default node's wake settings live at the top level. |
| Voice readiness | `turnstile/app.py` `ready` (`voice_port`) | Asked of the default node only. |
| Model names for outside services | `local_names` | Asked of the default node only. |
| Hard-coded names | `"compute"` / `'compute'` literals | turnstile 83, weightroom 67 (+19 in templates), loadcoach 20, benchmarks 16, db 11, others 5. |
| Stored names | `model_locations`, `machine_lifecycle`, `machine_key_rotations`, `events.machine`, `observation_*`, `node_discovery`, `external_tasks.machine`, `benchmark_runs.location` | Rows hold "compute" (or `''` for the default node). |

Batch lines already go through turnstile's own front door, so they follow chat placement.
Chat/API placement already spreads across nodes (`placement_order`), but a loaded model on a
busy node still beats an idle node that would have to load it.

## Phase 0: safety and inventory

Before any behaviour changes. Code and findings done 2026-11-12; backups are blocked (below).

- [ ] **Blocked:** take verified backups of the front's stores and each node's `loadcoach.db` and settings, using the existing backup routes. No machine can make one yet: the routes encrypt with `age` and read `data/backups/recipients.txt`. `age` is missing on jpk-main and jpk1–3. No machine has that `recipients.txt` (the front has one public recipient in `settings/recipients.txt`). The front's Backups page also only pairs the front with the default node, so jpk1–3 need `POST /turnstile/machines/{name}/databases/backups`. These backups gate Phase 1's deployment, not its code.
- [x] Every request that reaches the `pass_on` fallthrough is counted by route in [turnstile/fallthrough.py](../turnstile/fallthrough.py). The first of each route is printed to the journal once per process (`turnstile: default-node fallthrough METHOD route on port N`), with record IDs shown as `{id}` and at most 500 routes remembered. Printing instead of debug logging makes the journal show the live list once this is deployed.
- [x] [tests/test_default_node_inventory.py](../tests/test_default_node_inventory.py) sends every console request with a literal path through turnstile, using the live front's switches. It compares the routes that fall through with [tests/fixtures/default_node_fallthrough.txt](../tests/fixtures/default_node_fallthrough.txt): **31 routes** today, including `POST /loadcoach/tasks` (the CPU-node failure), `POST /loadcoach/schedules`, backups, file listings, request details, `stop`/`peek`, model setup and rename, and status. A new entry fails the test; a removed one asks for the list to be lowered. The live journal adds any route the console builds without a literal path.
- [x] The same test counts `"compute"` references per file against [tests/fixtures/compute_literals.json](../tests/fixtures/compute_literals.json): **240 in 49 files** (weightroom 94, turnstile 90, loadcoach 20, benchmarks 17, db 11, soundroom 3, mirrorwall 2, spotter 2, voice 1). More fails; fewer asks for the list to be lowered. `OWG_UPDATE_INVENTORY=1` rewrites both lists after a deliberate change.
- [x] Hash chain coverage: the event chain (`loadcoach/log.py` `chain_hash`, checked again by `db/transfer.py` `check_chain`) covers the `events` columns `at` through `text`, including `detail`. Machine names inside `detail` (task and schedule events record `machine=…`) are therefore never rewritten; Phase 2 resolves them through `node_aliases`. The `events.machine` and `events.source` columns are outside the chain and can be renamed. Renaming whole tables doesn't touch row contents, so it leaves chains valid.

## Phase 1: the setting and task ownership

This phase alone fixes the CPU-node benchmark runs. Built on branch `peer-nodes-phase1` (2026-11-12); not yet deployed (see the blocked backups in Phase 0).

Found while building: once the front keeps the task record, Status no longer listed a running benchmark probe, because turnstile dropped node entries whose ID is in its own batch store. It now keeps a node's benchmark entry while the front's record is in progress. Task creation also passes the node's own reason through when it can't freeze a model (for example "still being fingerprinted").

### Setting

- [x] Add `allow_caller_to_specify_destination: bool = True` to turnstile's `Settings` ([turnstile/app.py](../turnstile/app.py)). Mark it `NOW` under `turnstile` in `CHANGEABLE` ([loadcoach/settings.py](../loadcoach/settings.py)).
- [x] Label it in [weightroom/setting_labels.py](../weightroom/setting_labels.py): "Allow callers to choose the node", section `Requests and queue`. It then appears on the General settings page (`/settings`, [settings.html](../weightroom/pages/settings.html) via `settings_sections.html` and `settings_program.html`); no new markup. Help text: "Callers such as Node measurements and API requests with a `node` field may send work to a named node. When off, the front picks the node."
- [x] Keep it off System pages and out of the Compute nodes page (`hidden` set in `weightroom/app.py`).
- [x] Turnstile reads the current value from its settings-change store (`console_settings.changes` after the rename below), so a change applies without a restart.

### Task records always on the front

- [x] `FrontTasks.handle` always creates tasks; remove the `enabled` gate. Task IDs already owned by a node keep resolving there (existing rule: unknown IDs go to the legacy route).
- [x] Changed while building: a node's mode comes from its store, not from having a `front` address. A store with the `loadcoach.retirement` migration (jpk-main today) keeps no task, batch or schedule records and refuses to create them; any other store keeps serving its older records. jpk-main has a front address and still holds older tasks and two schedules, so a front-address rule would have hidden them. Turnstile never sends new task creation to a node either way.
- [x] Done without new read code: a store that is not retired keeps its normal `Batches`, so old runs, tasks and batches stay viewable, and turnstile forwards IDs it doesn't know to the node that has them. `FrontOwnedBatches` is now `NoLocalBatches`, used only by retired stores.
- [x] Old `front_tasks` keys (front `[turnstile]` and compute root) are ignored, with one startup log line saying so. They are left in the files.
- [x] Keep the `front-probe` dispatch and frozen subject checks unchanged.

### Pinned destinations

- [x] `benchmarks/app.py` and `FrontTasks.create`: if the setting is off and a target names a node, refuse with 400: "Choosing a node is turned off. Turn on 'Allow callers to choose the node' in Settings → General." Show it in the same `said` banner the run forms use now.
- [x] Fix the fallback in `start`/`queue_runs`: a location without its own `model_id` must never borrow the logical model's ID from another node.

### Wording

- [x] Remove "front-owned" from user-facing messages, settings help and docs ([operations.md](operations.md), [loadcoach.md](loadcoach.md), [installation.md](installation.md), `settings/examples/*.toml`). Say what happens instead, e.g. "the front keeps task records".
- [x] Rename code identifiers to say what they do. Proposed names:

  | Now | New |
  |---|---|
  | `FrontTasks` (turnstile/tasks.py) | `TaskRecords` |
  | `FrontOwnedBatches` (loadcoach/batches.py) | `NoLocalBatches` |
  | `FRONT_TASKS` (db/schemas.py) | `TASK_RECORDS` |
  | `front_tasks` variables and attributes | `task_records` |
  | `_front_task_owners` | `_task_record_owners` |

### Sibling switches

`front_settings` and `front_toollocker` come from the same earlier migration and use the same
unclear "front" wording. They are renamed the same way:

| Now | New | What it means |
|---|---|---|
| `[turnstile] front_settings` | `keep_console_settings_on_front` | The front stores and serves settings for its own programs (WeightRoom, turnstile, voice gateway). |
| `front_toollocker` (compute `loadcoach.toml`; WeightRoom's derived field) | `toollocker_runs_on_front` | ToolLocker runs on the front. Compute refuses ToolLocker settings and reports and doesn't manage `toollocker.service`. |
| `FrontSettings` (turnstile/settings.py), `app.state.front_settings` | `ConsoleSettings`, `app.state.console_settings` | Code names. |

- [x] Both keys keep their current meaning and values. Only the names change.
- [x] The update renames each key in place in the settings file after writing a `.before-peer-nodes` copy. Comments and every other line are kept.
- [x] The old key is still read if the new one is absent, with one startup log line. Rollback and hand edits keep working. If both are present and differ, startup stops and names both.
- [x] Update the startup checks and their messages (e.g. "toollocker_url requires port 8443 and keep_console_settings_on_front"), [operations.md](operations.md), [database-storage.md](database-storage.md), `settings/examples/*.toml`, `tests/test_source_docs.py` and the tests that use these names.
- [ ] Phase 2 removes the default node, so no compute node is left to hold the console's settings. From then on `keep_console_settings_on_front` is always true: the key is ignored, with a log line, and an explicit `false` stops startup with an explanation.

### Stored names

The rename also covers the database. One new numbered migration per store, run only after a
verified backup, idempotent, on SQLite and PostgreSQL:

| Now | New |
|---|---|
| table `external_tasks` | `benchmark_tasks` |
| table `front_task_evidence` | `benchmark_task_evidence` |
| table `front_task_finishes` | `benchmark_task_finishes` |
| table `external_task_reservations` | `benchmark_task_reservations` |
| migration stream `turnstile.tasks` | `turnstile.benchmark_tasks` |

- [x] The migration renames the tables in place (`ALTER TABLE … RENAME TO`) and moves the stream's recorded version to the new key in the same transaction, so existing migrations 1–3 are not run again.
- [x] The same tables on a node's legacy store (read-only history from Phase 1) are renamed the same way when that node updates.
- [x] Update every reader: `turnstile/tasks.py`, `loadcoach/tasks.py`, `loadcoach/batches.py`, `db/transfer.py`, `db/consolidation*.py`, `scripts/install-suite-support.py` and the tests.
- [x] Backups taken before the migration still restore: the rename is a migration (front stream migration 4, node `loadcoach.tasks` migration 3), so a restored older store is renamed at its next start. Tested on SQLite.
- [x] Rows inside hash-chained tables are not rewritten (Phase 0). Renaming a table doesn't change its rows, so chains stay valid; the test checks this.

### Tests

Done in [tests/test_node_choice.py](../tests/test_node_choice.py), with the two-node and legacy-ID cases in [tests/test_front_tasks.py](../tests/test_front_tasks.py). The PostgreSQL contract tests in `tests/test_postgresql.py` use the new names but were not run: they need `SUITE_TEST_POSTGRES_DSN_FILE`, and this machine has no PostgreSQL role for the test user.

- Task creation with targets on several nodes reaches each node (fake nodes), with the setting on.
- Setting off: pinned target refused with the new message; unpinned run placed.
- Legacy task IDs on a node stay readable after the change.
- Settings page shows the row under Requests and queue, saves with `manage-settings`, refuses without it.
- Table and stream rename on SQLite and PostgreSQL: fresh store, already-migrated store, rerun, and restore of a pre-rename backup.
- No `front_task`, `FrontTasks`, `FrontOwnedBatches`, `external_task`, `front_settings`, `FrontSettings` or `front_toollocker` name remains outside the rename migration and the old-key fallback.
- Settings files with the old keys, the new keys, both equal, and both different.

## Phase 2: every node is a peer

### Configuration

- [ ] `[turnstile] compute` becomes optional. All nodes are `[machines.NAME]` tables with `compute`, `spotter`, `machine_key_file` and optional `wake_mac`/`wake_address`.
- [ ] Migration in the existing OWG update path (no wrapper, no alternate tree): when the front's settings still have a top-level `compute`, write `settings/weightroom.toml.before-peer-nodes`, then move it to `[machines.<name>]`. Move top-level `wake_mac`/`wake_address` with it. `<name>` is the node's hostname (`jpk-main`), taken automatically with no prompt, so `--yes` works. If the hostname is already used by another node, or is not a valid node name, the update stops and says so, without changing anything.
- [ ] The node's `machine_name = "compute"` changes to the same name. The node's update writes `loadcoach.toml.before-peer-nodes` first. Keys, model paths and other settings are untouched.
- [ ] `owg pair` and `install.sh` write new nodes as `[machines.NAME]` only.

### Names in code

- [ ] Replace `["compute", *settings.machines]` and `settings.upstream()` with one list of configured nodes. Every call names its node.
- [ ] Remove the `pass_on` fallthrough to a default node. A request with no route and no node gets 404 "No route for this request" (Phase 0's test proves nothing legitimate hits it).
- [ ] Remove the `"compute"` allow-list entries as each module is converted.

### Stored names

- [ ] One numbered migration per store renames `compute` (and `''` in `benchmark_runs.location`) to the new name in the tables listed under *What still depends on the default node*. It runs only after a verified backup and is idempotent.
- [ ] Hash-chained columns are not rewritten. A `node_aliases` row (`compute` → `jpk-main`) resolves them when read.
- [ ] Request IDs: old unprefixed numbers resolve to the former default node through the same alias. New requests on jpk-main get its name prefix like any other node. Existing links keep working.
- [ ] Telemetry history keyed by host fingerprint needs no change.

### Console availability

- [ ] Replace the single `ComputeOff` state with per-node availability (`turnstile/machines.py` already tracks `ready`, `not answering`, `unknown`).
- [ ] The "compute is off" banner becomes a notice per missing node, using the shared alert macro in [parts.html](../weightroom/pages/parts.html), with the Wake action from [host_wake.html](../weightroom/pages/host_wake.html) when the node has wake settings.
- [ ] Pages that only need front data stop depending on any node being up.

### Voice and outside services

- [ ] Voice readiness asks every node that reports voice installed, not the default node. Today that is jpk-main and jpk1.
- [ ] `local_names` for outside services merges every node's model names.

### Voice sessions

- [ ] New setting `preferred_voice_node` on the General settings page, section `Speech`, changed with `manage-settings`. Its choices are the nodes that report voice installed. The migration sets it to the former default node (jpk-main), so voice behaves as it does today.
- [ ] A new session goes to the preferred node. If it is full (no free session room under its own limits) or not answering, the session goes to another node running voice, by the Phase 3 order. If none can take it, the existing "voice unavailable" answer is returned.
- [ ] A session stays on the node it started on until it ends.
- [ ] The preferred node is a preference, not a pin, so `allow_caller_to_specify_destination` doesn't apply to it. A caller naming a voice node follows the same rule as other requests.
- [ ] Tests: session on the preferred node; preferred node full → fallback; preferred node down → fallback; no voice node available.

### Tests

- Config migration: front and node settings, with backups written and every unrelated key preserved.
- Data migration on SQLite and PostgreSQL stores, including a rerun (idempotent) and hash-chain verification afterwards.
- Old request-ID links and old benchmark runs resolve to jpk-main.
- No `"compute"` literal remains outside the allow-list (target: empty).

## Phase 3: placement and overflow

Applies to chat/API requests, batch lines and admin calls that act on a model.

### Order

For each request, the capable nodes (Decisions table) are sorted:

1. **Can start now:** `free_now` is true (loaded, room, nobody waiting), or it `fits now` with no evictions and an empty queue. Within this group: loaded first, then the shortest estimated time to answer (`wait.seconds` from the node's facts), then name.
2. **Awake but busy:** would wait or evict. Sorted by estimated finish, then fewer evictions.
3. **Asleep with wake settings.**

Today's `placement_order` puts "loaded" ahead of "can start now". The new order puts "can start
now" first, so an idle node that must load the model beats a loaded node with a queue.

### Waking

- [ ] If no candidate can start now and a capable node is asleep with wake settings, turnstile sends the wake. It then holds the request (bounded by the existing `wait`, 180 s) until either an awake node can start it or the woken node answers. The request goes to whichever happens first. Requests already queued on a node are not moved.
- [ ] If no node has wake settings, the request queues on the best awake node, as today.

### Node hint

- [ ] Accept `"node"` in `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, image and audio requests. Remove it before forwarding.
- [ ] Setting off → 400 as in Phase 1. Unknown node → 404 "No node called X". Node lacks the model or it is disabled there → 404 naming the node. Node asleep → wake if it has wake settings, otherwise 503 with the node's state.
- [ ] Role routing (`turnstile/roles.py`, `loadcoach/roles.py` `pick`) uses the same order for the model a role picks.

### Tests

- Busy node with the model loaded and a queue, idle node with the same weights: request goes to the idle node.
- Model would evict another on node A and fits free on node B: goes to B.
- Different weights with the same name: never treated as capable.
- Node hint honoured, refused when off, refused for a node without the model.
- Wake path with fake nodes: holds, wakes, dispatches to the first ready node within `wait`.

## Phase 4: combined admin views

### Which data

Front-held data (accounts, groups, roles, conversations, collections, workflows, schedules,
batches, tasks) stays as it is. Node-held data is combined:

| View | Node data |
|---|---|
| Logs → Requests, Usage | Request log and token counts |
| Logs → Events, Observations | Event log, observation spans and metrics |
| Backups | Each node's backups and verification |
| Files | Each node's `files-index.json` (the General settings page already has a node picker for file locations) |
| Models | Already combined by `logical_catalogue`; keep |
| Status | Already combined by `combined_view`; keep |

Phase 0's fallthrough log decides the final list.

### How

- [ ] Turnstile asks every node in parallel with the no-wake header, merges the rows, and adds `node` to each. The answer also lists `unavailable_nodes` with last-seen time and whether each can be woken.
- [ ] Paged lists merge by their sort key with one cursor per node, so page two continues where each node left off.
- [ ] Writes carry the row's `node`. Turnstile refuses a write without one.
- [ ] UI: a Node column and filter pills from [navigation_parts.html](../weightroom/pages/navigation_parts.html) / [filter_parts.html](../weightroom/pages/filter_parts.html); offline notices and Wake buttons as in Phase 2. Polling refreshes never wake. Follow the template index in [AGENTS.md](../AGENTS.md#template-index) and run the component audit.

### Tests

- Merged order and paging across three fake nodes with interleaved timestamps.
- One node down: rows from the others, a notice with last-seen time, and a Wake button only for nodes with wake settings.
- Writes reach only the row's node.
- `test_ui_consistency.py` all-page checks, narrow layouts, empty and error states.

## Phase 5: backups per node

- [ ] Each node keeps its own backups with the existing `/loadcoach/backups` routes and retention.
- [ ] Backup schedules gain a node list (default: every node). Each occurrence creates one backup per listed node, with its own occurrence ID and result.
- [ ] The front's own stores keep their existing backup and appear in the same list as "Front".
- [ ] The Backups page lists every node's backups with status and verification, plus offline notices and Wake buttons.

### Tests

- A scheduled occurrence creates one backup per node, and retries only the failed ones.
- An offline node's backup shows as missed, not failed silently.

## Rollout

1. Phase 0 backups verified.
2. Commit and push each phase with its tests. All machines must run the same code: the front and every node are updated together.
3. After the owner's go-ahead, update and restart the front and each node with the established route, `owg update; owg restart`, over SSH. No pins, alternate trees, `PYTHONPATH` or wrapper launchers ([AGENTS.md](../AGENTS.md#deployment-workflow)).
4. On each machine, confirm the running service's source is the updated checkout at the pushed commit.
5. Live checks:
   - The setting appears in Settings → General and saves.
   - A `wl9-smollm-135m-import` load run pinned to each of jpk1, jpk2 and jpk3 queues and completes.
   - With the setting off, a pinned run is refused with the new message; turned back on, it works.
   - With jpk-main busy on a long request, a second request for a model on both jpk-main and jpk1 goes to jpk1.
   - Old request links and old benchmark runs still open.
   - Stopping jpk2's LoadCoach shows the notice on combined views without breaking them.
6. Rollback: restore `*.before-peer-nodes` settings and the Phase 0 database backups, then run the previous commit through the same `owg` route.

## Resolved review points

| Question | Answer (2026-11-12) | Where |
|---|---|---|
| How far should the rename go? | Everything: text, code identifiers, tables and migration keys. | Phase 1, Wording and Stored names |
| Which node takes a voice session? | A preferred voice node, with fallback to other voice nodes when it is full or down. | Phase 2, Voice sessions |
| Where does the default node's new name come from? | The hostname, automatically. | Phase 2, Configuration |
| Rename `front_settings` and `front_toollocker` too? | Yes. | Phase 1, Sibling switches |
