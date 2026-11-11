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

## 7. Dry fit split browser (2026-11-11)

Status: **implemented and locally tested; deployment and native qualification pending**. Selected proposal **02 / Split browser** is the live `/dry-fit` layout. The three sample layouts remain available with `python scripts/preview-fit-designs.py`; shared rows/bars live in `fit_parts.html` and `fit.css`. The [named-set and memory contract](loadcoach.md#validated-model-setup-and-presets-c05-milestone-93) is canonical for API payloads, limits, permissions, stale reviews, leases and failure recovery. This explicitly extends section 5's earlier primary-node-only scope.

<!-- fit-design-todo:start -->
### Implementation checklist

- [x] Reuse integrated-device classification in discovery, catalogue and Dry fit; retain unknown discrete memory as unknown (`loadcoach/gpu_type.py`, `weightroom/fit.py`).
- [x] Show separate physical VRAM/system RAM budgets, host headroom, other use, model-RAM policy exclusions and cgroup information; RAM-only nodes have one pool (`weightroom/fit.py`).
- [x] Account for engine host reservations and qualified offload evidence without treating RAM as automatic extra VRAM; keep exclusivity and per-process component allocations.
- [x] Ship the compact split browser for 30 local choices, independent search/type filters, memory/name sorting, bounded scrolling, stable-ID node drafts, exact/locked contexts and minimum-respecting auto-fit (`fit.html`, `fit.js`, `fit.css`).
- [x] Expose current-profile CPU RSS curves with conservative fixed first-load fallbacks (`Models.resident_measurements`); preserve focus, scroll, invalid edits and drafts during dynamic replacement and outages.
- [x] Save mixed LLM/audio/image sets with fixed media profile keys; validate engine/settings/file revisions and preserve existing LLM definitions without a database migration (`loadcoach/media_setup.py`, `loadcoach/loadouts.py`).
- [x] Add signed node-scoped GET/POST load-outs and keep every console action/redirect on its selected node; preserve lock/priority headers, caller permissions, no-wake behavior and same-name isolation (`turnstile/app.py`, `fit_page/fit_do`).
- [x] Retain fresh review tokens, joint admission, atomic settings, guarded undo, protected work and bounded lease cleanup/renew/release; extend tokens to node/resource policy and frozen media profiles.
- [x] Update API/source reference, CLI and diagnostic examples, README and walkthroughs; promote the shared templates in AGENTS.md.
- [x] Exercise authenticated 30-model browser flows, 1440/390/320 layouts, exact contexts, mixed profiles, signed two-node routing, outages, API limits, permission checks and existing lifecycle/engine tests. The all-page UI audit includes empty/error and dynamic states; existing source-reference docstring omissions are unchanged by this work.
- [ ] Deploy matching front/gateway/worker changes and perform operator-authorized native qualification. Commit/push does not deploy services. Keep prior assets and verified model-store/load-out backups; follow the canonical API contract's fixed-profile rollback precautions. Do not load or migrate real models merely to review the implementation.
<!-- fit-design-todo:end -->

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
