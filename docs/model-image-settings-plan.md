# Model, image, settings and benchmark changes

Status: **complete: implemented, tested, deployed and live-verified 2026-10-27**; [release and recovery receipts](../data/qualification/model-image-settings-20261027/report.json). The following sections retain the implemented contracts and recovery requirements. One missing/outdated pair is selected per schedule occurrence.

## Scope and current foundations

| Delivered change | Owning implementation |
|---|---|
| Compact Add a model | `weightroom/pages/models.html`, engine create-field templates and `weightroom/static/model-files.js`. |
| Workflow selector and ordinary fields | `mirrorwall/static/images.js::renderWorkflowPage` and existing versioned presets in `mirrorwall/store.py`. |
| SDXL workflow and negative prompt | Existing Studio, request validation and native `mirrorwall/runtime.py` path; bounded qualification below. |
| Dry fit by model type | `fit_node`, `fit_line`, `fit.html` and `fit.js`; fixed media profiles and retained language curves. |
| Shared encoder library | Cache/storage `encoders` roots, metadata/component discovery and `loadcoach/encoder_migration.py`. |
| Benchmark node selection | Explicit `{model, machine}` targets, shared location resolution and frozen run/task identities. |
| Missing/outdated schedule | `benchmarks/eligibility.py`, existing schedule worker and atomic reservations in existing stores. |
| System / Settings split | `weightroom/navigation.py`; existing URLs and permissions retained. |
| Image settings | Existing Turnstile administrator gate and separate Mirror Wall personal opt-in. |

Keep the current Turnstile identity/routing, LoadCoach admission, file transfer, benchmark worker, schedule worker and stores. This work adds no daemon, port or parallel scheduler. Voice consolidation is outside this plan.

## 1. Navigation and image settings

The pages use these navigation homes:

| System | Settings |
|---|---|
| Status, Telemetry, Alerts, Schedules, DB, Backups, Deploy | Compute nodes, Users, Groups, Tools, Plugins, Voice, Audio, Image, General |

- “General” is the existing Settings page within the new Settings category. Retain existing URLs where practical, including compatibility for `/system/voice` and `/system/audio`; change their navigation ownership and breadcrumbs.
- Preserve each page's existing permissions. Restricted categories disappear when none of their pages is accessible. Preserve detail-page highlighting and mobile navigation.
- Add **Settings → Image**. Move the administrator adult-preview permission out of the generic Turnstile form into this page while retaining the same setting authority and API.
- Inventory other actual image-wide settings and surface them here. Do not turn per-model workflow bounds, GPU allocation or node-specific loading controls into global settings. Link to those owning pages instead.
- Keep global permission and personal adult-preview opt-in distinct, both default-off. Retain personal access to the existing opt-in when globally permitted, with a link from Civitai to Image settings; moving an administrator control must not enable previews for everyone.

Acceptance: all requested pages appear in the correct category; direct URLs and permissions still work; the Image page changes the existing setting immediately and safe-preview enforcement remains intact.

## 2. Compact model creation and Civitai cards

### Add a model

Desktop layout:

```text
Compute node [Show node] | File                     | Engine
Name                    | Minimum context          | Maximum context [Add]
```

- Combine the visual layout without nesting forms. Keep the selected node, file inventory and enabled engine list consistent across node/engine changes and validation failures.
- For a selected language-model file with valid trained-context metadata: set maximum to that value and minimum to `max(1, trained_context // 2)`. Update these defaults when another file is selected; allow subsequent manual edits.
- If metadata is missing or invalid, leave context fields for explicit entry rather than guessing. Keep server-side context and engine validation authoritative.
- Remove the **Cards** and **Allow model to split** creation controls. New llama.cpp entries explicitly receive `gpus="any"` and `allow_split=true`, including non-JavaScript submission. Existing model/node configurations are not reset; their detailed placement controls remain available.
- Respect different engine contracts. Strata still needs its tokenizer and bounded supported context; do not apply unsupported llama.cpp placement/splitting fields to it. Show necessary engine-specific fields compactly below the common rows.
- Stack naturally on narrow screens. Preserve cache/storage capacity checks, bundle promotion, destination copying and duplicate-name handling.

### Civitai

- Rename **Source & license** to **Model page**, retaining the existing destination link.
- After **Choose file**, label the destination selector clearly and explain in that card: **Image cache** is the active image-model cache; **Image storage** is the retained library, with verified promotion to cache before use.
- Use friendly labels for `image` and `storage_image`; retain canonical place identifiers internally. Offer only configured destinations and retain hash checks and download progress.

Primary files: `models.html`, engine `create_fields.html` templates, `model-files.js`, `engines/llama_cpp/form.py`, `mirrorwall/static/discovery.js` and related route handlers.

## 3. Encoder feasibility and library migration

### What can be shared

**Some image text encoders can serve another purpose, but this is not universal drop-in embedding support.** SDXL uses CLIP/OpenCLIP text encoders; CLIP implementations expose standalone text features. That establishes a possible second use, not compatibility of every installed component file with Suite's current embedding engine. Sources: [SDXL model card](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0), [CLIP text features](https://huggingface.co/docs/transformers/model_doc/clip), [OpenCLIP encoding example](https://github.com/mlfoundations/open_clip).

For each candidate, establish architecture, file format, required tensors, tokenizer, pooling/projection, normalization, vector dimension and input limit. Image-conditioning hidden states and retrieval vectors have different contracts. A Qwen/Mistral image component is not automatically an embedding-tuned model or a runnable chat model.

- Inventory dedicated embedders and detached image text encoders across every configured node and both cache/storage tiers. Derive roles from metadata and configured consumers, not filenames alone.
- First use an existing LoadCoach engine where genuinely compatible. Qualify one representative existing candidate for standalone embedding if possible; expose the second capability only after load, vector-shape, repeatability and bounded retrieval/similarity checks.
- If a candidate needs an unsupported runtime or adapter, report that boundary explicitly. A new optional adapter within LoadCoach would be a separate scoped decision; do not add a service or silently advertise support. Existing embedding models remain usable.
- Shared files do not imply shared GPU residency across processes. Account for each admitted engine's actual allocation; deduplicate disk storage, not memory reservations.

### Storage changes

Add `encoders` and `storage_encoders`, mapped to `MODELS_ROOT/encoders` and `STORAGE_ROOT/encoders`. Keep a complete model/package subdirectory, including required tokenizer/configuration files. Move dedicated embedding weights from `llm` and detached image text encoders from `image`; leave diffusion weights, VAEs, projectors and whole checkpoints with embedded encoders in their appropriate existing places.

Migration sequence:

1. Inventory every configured node, all model/component references, pending transfers/downloads and active work. If a node cannot report usage, defer the global move; unknown is not unused.
2. Produce a machine-readable old/new path and consumer manifest. Detect destination conflicts, verify available room and protect complete packages, shared dependencies, active models, holds and locks.
3. Extend roots, discovery, permitted file types, downloads, component pickers, model installation/distribution and cleanup protection before relocating files. In particular, GGUF image encoders must not lose their component role when no longer under an `image` directory.
4. Copy into a temporary destination, verify every file by SHA-256 and install atomically. Rewrite current node/model/component references through existing authorities; retain stable model IDs and historical records.
5. Check file-selection revisions and path-keyed measurements explicitly. Current fingerprints include a selected-file revision: a relocation must not silently rewrite historical fingerprints or mark stale evidence current. Preserve valid measurement provenance only where equivalence is established.
6. Verify normal embedding and image loads from the new paths. Remove superseded copies only after all affected consumers and nodes are verified. Retain the relocation manifest and a tested way to restore old paths/references; keep verified storage/library copies protected throughout.

Primary files: `loadcoach/config.py`, `loadcoach/files.py`, `loadcoach/models.py`, `mirrorwall/assets.py`, `mirrorwall/components.py`, engine file-selection helpers, `turnstile/model_install.py`, artifact distribution/cleanup, Files/Hugging Face/Civitai UI, installer defaults and installation docs.

## 4. Image workflow forms and SDXL qualification

- Replace the JSON-card list with a **Workflow** select box by name and one editing form. Include New workflow, associated image model, version, save-new-version and permitted sharing controls.
- Use the selected model's family, defaults, capabilities and limits to render fields: dimensions, count, steps, seed, sampler/scheduler, relevant guidance/CFG, supported image-to-image strength, reference preparation, upscaling and configured LoRA choices. Hide unsupported fields and validate again on the server.
- Preserve existing workflow IDs, versions, ownership and publication permissions. Preserve allowed settings during round trips; continue refusing private references, arbitrary paths and model overrides inside preset settings.
- Expose negative prompt for SDXL in both the workflow form and generation form; save/apply/reuse it consistently. Flux workflows continue to reject non-empty negative prompts. Reuse the existing native negative-prompt path rather than adding another inference route.
- Inventory installed SDXL-compatible checkpoints and complete components first. Prefer an existing compatible checkpoint; if none is available, use the official SDXL Base 1.0 checkpoint as the proposed baseline through existing verified downloads. Record the exact selected files, hashes and model revision.
- After active/queued benchmark work has drained, configure and load SDXL through LoadCoach with an explicit node/resource profile. Qualify the full generation envelope, not file size alone: GPU peaks, host RAM, output format, cancellation and cleanup.
- Save a clearly named reusable SDXL workflow and verify selection, edit, save, reload and generation. Confirm the negative prompt reaches the native request and perform a controlled fixed-seed with/without-negative comparison. Distinguish functional checks from subjective image quality.

Primary files: `mirrorwall/pages/mirrorwall_workflows.html`, `mirrorwall/static/images.js`, `mirrorwall/model.py`, `mirrorwall/requests.py`, `mirrorwall/front.py`, `mirrorwall/runtime.py` and preset storage.

## 5. Dry fit by model type

- Add **LLM / Audio / Image** pills while preserving the selected compute node. Embedding models remain within the LLM family for this three-pill layout, with their embedding identity shown.
- Include configured image models from the selected node, even when not measured. Show unknown/unqualified memory explicitly rather than treating it as zero.
- Keep LLM context curves and auto-fit behaviour. Audio/image models use their measured fixed profile/envelope with context shown as not applicable; do not invent a token slider or interpolate their memory from context zero.
- Draw appropriate per-card and host-RAM reservations, preserve exclusive-card rules and avoid double-counting an image profile's components. Filtering the table must not silently remove already selected models from the combined fit preview.
- Keep Dry fit read-only: selecting models does not load, move or benchmark them. Do not broaden primary-node-only named load-outs as an accidental side effect.

Primary files: `weightroom/app.py::fit_page/fit_line/fit_node`, `weightroom/pages/fit.html`, `weightroom/static/fit.js` and engine measurement summaries.

## 6. Explicit benchmark nodes and scheduled missing work

### Shared node selection

- Add a node dropdown to **Start a run** and benchmark schedule forms. Each model row shows its actual target node, with a selector for its eligible available locations.
- Prefer the chosen node. If a verified catalogue shows that the model is absent there, preselect an eligible available location and make the fallback visible before submission. An offline/unknown node is not proof that the model is absent.
- Submit explicit per-row `{model, machine}` targets for quality runs as well as measurements. Freeze resolved node, local model ID, model fingerprint and engine/machine environment into the queued task and retained run; workers must use that node. Refuse or defer if it becomes unavailable rather than silently switching after queueing.
- Reuse this resolution logic for schedules, recording the preferred node, resolved node and fallback reason in occurrence history. Preserve old fixed-model schedules with compatible defaults and retained scoped grants.

### Missing/outdated schedule

- Add a schedule kind for **Run one missing/outdated benchmark** using the existing schedule worker and benchmark queue. Let the owner scope eligible models/benchmarks and choose a preferred node; an all-permitted selection remains bounded by current permissions and enabled definitions.
- Reuse/refactor the existing outstanding-work eligibility and `evidence_matches` logic. Treat evidence as current only for the correct stable/local model identity, node, effective model fingerprint, benchmark version/protocol/grader/case set, server mode and engine/machine hashes.
- Re-score formula-only changes from retained metrics when possible; do not require native reruns merely for a new display formula or model name/order.
- At each due occurrence, select **one** eligible missing/stale pair, prioritizing the oldest eligible gap/least recently attempted work with a stable tie-break and failure cooldown. Persist the chosen pair and frozen target before submission so restarts or lost replies cannot choose a second pair for the same occurrence.
- Exclude matching creating/queued/running work across manual and scheduled submissions. Use atomic selection/reservation in the existing stores; retain the schedule's stable occurrence/task IDs, revocation checks, overlap/missed-run policies and low-priority execution.
- When everything is current, record “Nothing outstanding.” When availability is unknown, report/defer it; do not fabricate freshness or wake hosts. Do not count failed, cancelled or incomplete runs as successful results.
- Preserve capability rules: LLM quality tests, compatible embedder measurements, and image/audio loading measurements only. No image/audio quality or throughput benchmark expansion.

Primary files: `benchmark_start.html`, `weightroom/app.py`, `weightroom/static/benchmarks.js`, `benchmarks/app.py`, `benchmarks/store.py`, `benchmarks/worker.py`, `weightroom/schedules.py`, `loadcoach/schedule_time.py`, `loadcoach/schedule_grants.py`, `turnstile/tasks.py` and schedule templates/schema migrations where needed.

## 7. Dry fit redesign proposals (2026-11-11)

Status: **design previews only; production changes below remain unchecked**. Render with `python scripts/preview-fit-designs.py`, then open `build/fit-designs/index.html`. Revised for **15–30 available models**: each sample node now has 30 distinct choices. **Selected rows** (recommended) uses a searchable multi-select picker and tunes only selected models; **Split browser** keeps the compact catalogue beside selected rows; **All-model sheet** uses one bounded table with collapsed unselected rows. Search, type filters, memory/name sorting and independent list scrolling keep catalogue size from expanding the page. The shared state retains node drafts, exact LLM contexts, context locks, fixed audio/image envelopes, qualified hybrid RAM, browser-saved versions and API review. Additional names and profiles are illustrative, not engine compatibility claims. Browser-local saved samples never create real load-outs. These proposals explicitly extend the earlier primary-node-only scope in section 5; that delivered behavior remains unchanged until implementation.

<!-- fit-design-todo:start -->
### Implementation TODO

- [ ] **Normalize allocatable devices.** In `weightroom/app.py::fit_page/fit_node` and the catalogue/status producers, stop treating every `known.cards` entry as usable VRAM. Reuse the intent of `inventory_rows`' existing PCI-only AMD/Intel filter, but expose explicit device kind, independent-memory status and engine eligibility instead of relying on name prefixes. RAM-only nodes show system RAM alone; integrated/shared memory is never counted a second time as VRAM. Unknown dedicated-GPU telemetry stays visible as unknown, not zero or infinite capacity.
- [ ] **Always expose relevant host RAM.** Replace `fit_page`'s model-dependent `cpu_available` gate and `fit.html`'s empty-card message with node capacity. Draw system RAM on CPU nodes and whenever a selected engine can use it. Reconcile physical total, OS/other use, host headroom, configured model-RAM limit, hybrid reservations and the separate cgroup ceiling using `loadcoach/runner.py::host_memory` and `fit_cpu_limit`. Avoid subtracting existing model residency twice; show stale/offline telemetry and block a positive fit verdict when capacity is unknown.
- [ ] **Qualify spillover per engine and placement.** Inspect `engines/contracts.py`, `engines/llama_cpp/adapter.py`, `engines/strata/resources.py`, `loadcoach/runner.py` and measurement summaries. Expose permitted placements and RAM/VRAM reservations at each supported context/profile. Free RAM is not automatic extra VRAM: require a supported hybrid/offload configuration and account for its full reserved host budget. Keep exclusive-card constraints, multi-GPU distribution and process-specific shared-component memory; no deduplication merely because files are shared.
- [ ] **Unify the selection editor for 15–30 local choices.** Replace separate GPU/CPU controls in `fit.html`/`fit.js` with one stable-ID selection per local model and an explicit placement. Use compact rows, a searchable multi-select catalogue, independent catalogue/selection filters, selected totals across hidden types, name/memory sorting and bounded list scrolling; keep unselected entries out of the tuning area or collapsed in the all-model sheet. Reuse canonical components and DOM templates. Keep drafts per node, local availability and model access; preserve keyboard focus and list scroll when toggling a choice. Add exact integer context entry alongside sliders, measured bounds, context locks and a fair auto-fit that reports an impossible minimum rather than silently dropping below it. CPU context memory currently uses a constant `fit_cpu_mib`; add qualified curves or clearly label conservative fixed reservations. Preserve `data-hold-refresh`, focus and errors across dynamic replacements. The existing API permits 1–16 members per named set: distinguish catalogue size from this limit, surface the server's bound explicitly, and change it only with matching bounded validation/admission tests.
- [ ] **Save mixed engine sets correctly.** `fit.js` currently skips `data-fixed` image/audio rows in both GPU and CPU save paths. Extend `loadcoach/loadouts.py::save/configuration`, engine setup previews and `model_loadouts`' versioned JSON contract for discriminated LLM context versus image/audio profile references. Preserve stable model IDs, exact cards/placement, engine fingerprint and profile revision; never invent positive token contexts for media. Validate all members together, missing/renamed models, exclusivity and conflicting/stale profiles. Add a compatible schema/data migration only if needed; retain existing LLM definitions and optimistic replacement checks.
- [ ] **Route named sets to the selected node.** `fit_page` currently lists sets only on `compute`; `fit_do` always POSTs to `/loadcoach/loadouts` and redirects without `machine`. Add node identity to every save/review/apply/request/renew/release/delete/undo form and response. Proposed gateway extension: GET/POST `/turnstile/machines/{name}/control/loadouts`, forwarding to that node's existing `/loadcoach/loadouts`. This route pattern exists, but `turnstile/app.py::machine_control` currently permits only status/drain/clear/unload, so the loadouts action is **not implemented**. Keep definitions in each node's existing store; isolate same-named sets, review tokens and leases by node, with no silent fallback to the primary.
- [ ] **Keep loading reviewed and bounded.** Reuse `loadcoach/app.py::loadout_control` and `loadcoach/loadouts.py`'s preview → `request` token flow, management/load/member permissions, joint admission, protected work, 600-second leases and guarded undo. Bind reviews to node/version/settings/profile and relevant capacity freshness; revalidate on request. Extend gateway no-wake behavior and timeout handling for the proposed path, preserve caller identity/origin checks, and handle partial failures without leaking set holds. Selecting, tuning and saving never load models.
- [ ] **Document the actual API contract.** Update `docs/loadcoach.md`'s named load-outs reference, `weightroom/system_docs.py` route descriptions/permission and side-effect annotations, diagnostic examples in `loadcoach/test.html`, CLI help/examples in `loadcoach/cli.py`, `docs/README.md`, `app-walkthrough.md` and `docs/agent-walkthrough.md`. Describe node-scoped list/save/preview/request/release, mixed-engine payloads, warning acknowledgement, 409 stale review/version, unavailable nodes, permissions and lease lifetime. For the primary node the existing API already loads a named set with `POST /loadcoach/loadouts` + `action: request`; do not add a redundant unreviewed load endpoint. Mark proposed routes unavailable until shipped.
- [ ] **Verify before release.** Extend `tests/test_fit_page.py`, `tests/test_loadouts.py`, `tests/test_loadouts_browser.py`, `tests/test_model_image_settings_browser.py`, gateway machine-control/no-wake tests and `tests/test_ui_consistency.py`. Cover 30-model catalogues, large selections, list-height bounds, search with no matches, filter/sort persistence, keyboard selection near the bottom, RAM-only with misleading integrated graphics, dedicated GPU + RAM, unknown/offline memory, unsupported spillover, dual GPU, min-context overcommit, locked/exact contexts, media profile changes, mixed sets, two nodes with the same set name, reload persistence, stale tokens, permission loss, renew/release/undo and partial failure. Browser-check all proposals and chosen production layout at 1440/390/320 pixels in light/dark, including empty/error and dynamic states. Run the shared component audit and affected feature suites; shared-component changes also require the full all-page browser checks.
- [ ] **Ship the chosen layout together with its backend.** Update the AGENTS template index, deploy only matching affected front/gateway/worker changes through existing release procedures, and preserve prior assets plus load-out data/schema recovery. Verify one bounded approved mixed set on each supported node type; retain existing definitions and do not migrate or load real models merely to view the proposals.
<!-- fit-design-todo:end -->

## 8. Benchmark Runs redesign proposals (2026-11-11)

Status: **03 / Run studio implemented locally with a compact picker for 15–30 models; deployment and live inference not performed**. The three sample-only proposals remain available through `python scripts/preview-benchmark-designs.py` → `build/benchmark-designs/index.html`; their illustrative fleet is not a live inventory. The production UI and batch/recovery contract are documented once in [benchmarks/README.md](../benchmarks/README.md).

| Proposal | Best use | Structure |
|---|---|---|
| 01 / Comparison lab | Repeated evaluation across models and nodes | Model × node selection with all-node overview |
| 02 / Machine lanes — previous layout | Planning around individual machines | All-node overview, running work, queue, node-local setup, filtered history |
| **03 / Run studio — selected and implemented** | Preparing a deliberate batch | Bounded model picker beside running work and queue; history below |

### Implementation checklist

- [x] Adopt Run studio in `benchmark_studio.html`/`benchmark_nodes.html`, reusing `parts.html`, navigation and the canonical `benchmark_parts.html` model rows, node choices, evidence and run controls. Show one row per model in a bounded, searchable list with a sticky header and selection by model or node; cap queue height and disclose logs on demand.
- [x] Use gateway node topology independently of compatible models; retain front, empty, disabled, unknown and unreachable nodes. Consume choices only from the authorized catalogue, never the monitor's model list.
- [x] Keep running work, queue, setup and terminal history expanded, with recorded node/test/version/identity, detail links and controls; preserve existing POST names and HTMX fragment IDs.
- [x] Review up to 100 exact model/node targets, preserve per-test selections across dynamic replacements and searches, and preserve unapplied history filters during polling. Revalidate permissions and targets server-side; return per-target outcomes and stable run IDs.
- [x] Support compatible LLM/embedding tests and media load-only choices; keep node-local evidence and honest unknown inventory. No inferred capacity or fabricated throughput.
- [x] Retain **globally serial execution**; no node-parallel worker extension. Render the current active list without duplicating running work in the queue.
- [x] Retain backend global-pause semantics and label the control **Pause all**; it yields running work and retains completed samples.
- [x] Add bounded history pagination, model/test/reason search, node/outcome filters and “Set up again”; preserve UTC storage and existing local-time rendering.
- [ ] Deploy through the existing release procedure after review; preserve prior code/assets for rollback. No schema migration or inference run is required to install this UI.

### Code review findings and implemented fixes

Scope: benchmark page/forms, setup/live/queue/history fragments, selection behavior, view helpers, internal API submission, reservations and acknowledgement recovery. All ten original benchmark findings are addressed below.

| Priority | Original finding | Implemented fix / regression coverage |
|---|---|---|
| P1 | Batch could partially succeed while reporting generic failure. | `queue_runs` retains each task outcome and continues independent targets; the page reports accepted, active, failed and unconfirmed counts. API tests reject the second target after accepting the first. |
| P1 | Malformed target JSON silently discarded valid explicit targets. | Browser submissions reject malformed/non-object/mixed or oversized target lists before forwarding any target; regression tests assert zero submit calls. |
| P1 | Changing a quality node left the old node's score/date/flags visible. | Removed the quality location dropdown; each compact node choice binds its own `(model, node, test)` evidence and exact target. Different-node evidence is tested independently. |
| P2 | Nodes disappeared when no eligible model existed there. | Configured topology drives both overview and lanes; privacy and empty/offline/disabled/front-node tests cover nodes with no choices. |
| P2 | Running work appeared again in the queue. | Separate active and waiting lists; shared controls stay available on active cards. A live-detail outage retains the running summary and other page sections. |
| P2 | Empty selections could be submitted. | Queue/review is disabled for zero or over 100 eligible targets, including embedder setup; server validation remains mandatory. |
| P2 | Fully deduplicated batches still said “Queued.” | API returns existing run IDs and the UI reports “Queued 0 runs” plus the active count; paused/creating reservations also exclude duplicates. |
| P2 | Queue/history omitted node identity. | Both show recorded nodes with legacy primary-node fallback; history can filter by node and prepare the same target again. |
| P2 | Active-detail failure replaced the whole page with an error. | Live-detail failures render a scoped retry notice alongside the retained run identity; queue, setup and history remain usable. |
| P3 | Quality setup's empty row spanned four of five columns. | Removed the obsolete quality table. Embedder matrix empty rows continue to derive their actual node-column count. |

Additional recovery hardening retains uncertain submissions in `creating` rather than freeing their reservations. Restart reconciliation reads the original task ID/token without resubmitting; pause/resume is refused until acceptance is known. Tests cover a lost acknowledgement, restart, duplicate retry, and recovery of the same task. Browser tests cover fast test changes before HTMX settles, focus-preserving searches, selection review, polling and 1440/390/320 layouts.

Run studio validation: **68 targeted checks passed**, including the shared component audit, benchmark API/progress suites and browser cases with 15/30 long-named models, one active run and 12 queued runs at 1440/1024/390/320 pixels in light/dark. Tests verify bounded scroll regions, sticky headers, per-model/per-node selection, search-hidden targets and exact batch review. The full UI audit passed 32 checks; its six all-page size checks flag unrelated Users-page heading, padding and Show-button sizing, with no benchmark-page failures. Temporary `age` binaries satisfied the fixture dependency without changing system packages. `git diff --check` and Python compilation pass.

## Verification and delivery

Use focused checks per phase, then the required repository checks before release:

- Model creation: exact desktop field order, context metadata/defaults, manual edits, missing metadata, engine switches, backend placement defaults, capacity refusals and node persistence.
- Navigation/settings/Civitai: permissions, old URLs, direct/detail highlighting, administrator versus personal preview settings, Model page links and destination explanations after Choose file.
- Encoder migration: offline nodes, shared consumers, full bundles, hash mismatch, insufficient capacity, conflicting destinations, interrupted moves, fingerprints/history and rollback; prove both intended consumers use the new path.
- Workflows/SDXL: family-dependent forms, saved-version round trips, negative-prompt forwarding, validation and live bounded SDXL generation with recorded resource/cleanup evidence.
- Dry fit: measured/unmeasured image/audio models, no token sliders for them, node selection, exclusive placement, shared-component accounting and combined selections across pills.
- Benchmarks/schedules: preferred and fallback nodes, quality/resource target routing, per-node evidence, renamed/recreated models, protocol/engine changes, all-current no-op, disabled/unauthorized work, duplicate claims, lost replies, restarts, failure fairness and unavailable nodes.
- Chromium at desktop, 390 and 320 pixels; verify real controls and table overflow, not only HTML markers. Retain separate implemented, tested, deployed and live-verified status.

Update existing owning guides, API reference and walkthrough alongside implementation. Use small scoped commits; never push. Stage reviewed immutable releases with matching front/worker/schema changes, wait for affected work to drain and preserve current configuration, credentials, native runtimes and resource limits. Deploy only affected services and retain per-phase recovery records. The owner subsequently authorized execution, encoder migration, qualification and deployment on 2026-10-27.
