# Image generation integration plan

Status: implementation deployed on the control node and main compute; frozen-build GPU qualification and regression checks are complete. Flux2Dev/klein, references, LoRAs, upscaling, private assets, presets, queued jobs, live previews, sweeps, pause/resume and composite masks are implemented. Civitai authentication and the global/per-user preview policy are live-verified; owner visual quality review remains separate.

## Confirmed requirements

- Bring the necessary igen workflow behavior into Suite using stable-diffusion.cpp (sdcpp) for generation, omitting unrelated application tools.
- Keep the majority of image implementation in the root `mirrorwall/` package (Mirror Wall): engine/workflow logic, assets/storage, discovery and UI assets. Existing Suite modules contain integration hooks into their established authorities.
- Make sdcpp installation optional: offer it during initial installation, provide a script to add it later and document manual installation.
- Add image model and workflow management under Models, separate from language-model configuration.
- Add an image-generation window under Chat with editable image settings, reference-image uploads and an output browser with thumbnails and a larger selected-image view.
- Expose OpenAI-compatible image generation through Turnstile and the existing worker admission path.
- LoadCoach accounts for the complete workflow, including reference preparation, component loading/offloading, generation and enabled post-processing.
- Configure workflows for one or two GPUs, including true diffusion-model splitting to support larger models; account for each card and host RAM.
- Track image-model assets and dependencies in Files/cache/storage; add Civitai discovery/download alongside Hugging Face.
- Prepare reference images automatically, including resizing and model-based enlargement where required.

## Existing igen behavior to preserve or deliberately revise

Source checkout: `/home/jpk/ai/igen`. Read `README.md`, `HELP.md`, relevant `SPEC.md` sections and all of `WORKPLAN.md`; cross-checked the implementation paths below. Measurements in those documents are historical igen evidence, not Suite qualification. The work-plan header and several earlier design sections are superseded by later notes or code.

| Consideration | Current implementation / implication for Suite |
|---|---|
| Reference preparation | `tools/refprep.py`: small inputs receive one native-factor model upscale followed by Lanczos to the target; large inputs are downsampled; inputs already at target are skipped. The interactive Prepare references action is separate from generation today. Suite must make it automatic. |
| Reference dimensions | `igen/catalog/images.py:fit_megapixels` preserves aspect ratio with multiples of 8. Suite uses multiples of 16 to satisfy native Flux VAE/latent packing; 8-only dimensions such as 816×616 triggered a native reshape assertion during qualification. Reference megapixels and output dimensions remain separate. |
| Repeated preparation | Prepared dimensions are intended to match generation preprocessing. Preserve originals and avoid repeated enhancement/resampling; test the fixed-point behavior rather than assuming it. Existing preparation replaces the working reference and moves its original to `.backup/`; Suite should use immutable originals and derived assets. |
| Correct upscaler kind | `HELP.md` and `igen/registry.py`: use image upscalers such as 4xNomos8kSC or 4x-UltraSharp for reference preparation. Face restoration models such as GFPGAN are a different asset kind and must not be selected for this step. |
| Upscaling implementation | `igen/catalog/upscale.py`: native model factor, optional final Lanczos resize, 512-pixel tiles and 96-pixel overlap. These are existing defaults to qualify, not universal settings for every upscaler. The implementation also allocates stitched output/weight tensors, so tile size alone does not bound whole-operation memory. |
| Reference meaning | `SPEC.md` section 3.2 and `igen/registry.py`: SDXL img2img and instruction-driven Flux reference generation are distinct capabilities. Current Flux graph accepts zero, one or two references; order matters. Reference slots can represent people, outfits or other prompt-defined roles. |
| Prompt helpers | `HELP.md` and application configuration: separate helper text for zero, one or two references. Carry required image-model instructions into workflow configuration without importing igen's standalone language-API settings or credentials. |
| Quality limits | `WORKPLAN.md` later face/hand notes: hand repair was tried and dropped; crop redraws removed scene details and did not reliably repair finger counts. Do not reintroduce it as a supported repair workflow. |
| Face and hires behavior | Current `tools/post.py` runs upscale -> faces -> hires -> optional filter. `post.protect` restores detected faces after hires; current face modes include klein, SDXL, reference-conditioned modes and restoration models. Earlier docs describe a different order. Reference-conditioned face replacement had identity/pose tradeoffs; no automatic claim that it preserves identity. |
| Post defaults | Current `tools/stream.py`: every optional post step is off; face protection is on when hires uses it. Face blend defaults to 0.5. Enabling face reconstruction or whole-image diffusion is distinct from reference resizing. |
| Batch efficiency | Existing post-processing batches each stage across images to reduce model reloads. Preserve reuse where it fits Suite queue fairness; do not hold an unbounded batch ahead of interactive work. |
| Provenance | Current code embeds resolved generation/post settings in PNG metadata, superseding SPEC's original JSONL-only design. Suite needs stable asset references and hashes rather than workstation paths; embedding prompts/references in exported images is an owner decision. |
| Components and precision | Current loaders support staged components and per-component precision. SDXL uses a bf16 VAE with tiling to avoid fp16 NaNs. Required tokenizers/configs currently fetched on first load must become tracked dependencies. Retain needed ingest/conversion paths for supported formats. |
| GPU/RAM ownership | `igen/backends/memory.py`: the encoder can opportunistically occupy a second visible GPU; its main manager does not account for that card in the same way as the primary. Replace opportunistic placement with the admitted profile. Preserve safe node-boundary eviction and bounded RAM-aware offload/OOM handling. |

## Integration contracts

### Engine and admission

Extend the existing [engine contract](../engines/contracts.py) and [LoadCoach runner](../loadcoach/runner.py) with an sdcpp adapter. Run the native runtime as a supervised child with a private local interface. Reuse the necessary igen orchestration, reference preparation and asset-conversion behavior around it. Keep one admission authority and existing identity, signing, lifecycle and logging paths.

The owning implementation is `mirrorwall/` at the Suite root. Organize image engine/model/request contracts, supervised runtime, control-node-owned asset/workflow persistence, discovery and image UI/templates/static assets there. Installer entry scripts, the engine registry, control node/worker route wiring, navigation and shared Files/admission changes remain small integrations in their existing locations. Package and deploy Mirror Wall with the relevant control node/compute installations.

Each execution profile declares component placement, diffusion split mode, permitted input/output sizes, precision, host-RAM budget and required GPUs. Distinguish single-card execution, two-card component placement and true diffusion splitting across two cards. Prioritize layer-split qualification for larger-model capacity; qualify row splitting separately where the compiled backend supports it. Expose only assigned GPUs to the child and reserve the complete layout before preparation/loading begins.

For a supported transformer diffusion model, a layer-split profile uses an explicit device list such as `diffusion=cuda0&cuda1` with `--split-mode layer`. Verify the effective split in runtime diagnostics and per-card allocations: an upstream fallback to the first device must not count as a successful two-card profile. Reject unsupported requested layouts clearly. Two cards' VRAM is not a flat combined pool; qualify the first card's non-block weights, text encoder/VAE placement, activations, buffers and reference/upscale stages within each card's budget. Performance improvement is separate from the required capacity benefit.

Uneven diffusion placement is acceptable; equal card utilization is not a requirement. Preserve other resident models whenever a qualified profile's full per-card peaks and configured headroom fit beside them. Unmeasured profiles remain exclusive during qualification. Sharing requires a reproducible split/resource envelope, including loading, reference preparation, sampling and upscaling, rather than weight sizes alone. Track reserved versus observed usage separately. Do not release a reservation merely because a stage offloads weights; confirm cleanup before releasing resources after cancellation/crash. Profile identity includes workflow/model/component versions, layout, split ratios/budgets and input-size bounds.

### Optional installation and maintenance

Extend the existing [Suite installer](../scripts/install-suite.sh) and its [support/audit code](../scripts/install-suite-support.py). The implemented interfaces are:

- **Initial installation:** when LoadCoach is selected (including compute/all presets), ask “Install optional sdcpp image-generation support?” before the final action summary. Recommend default No on a fresh install. Detect an existing compatible installation and offer to keep it. Control-node-only installations explain that the runtime belongs on the selected compute host.
- **Unattended installation:** add explicit install/skip/keep choices, `--sdcpp install|skip|keep`. Omission preserves an existing installation or skips a fresh one; `--yes` alone must not enable optional downloads/builds. Include the selection, dependencies, backend and target paths in `--dry-run`; `--check` audits without installing or starting a model.
- **Install later:** add an independently runnable, idempotent `scripts/install-sdcpp.sh`, called by the main installer when selected. It discovers Suite paths and existing binaries, checks prerequisites, selects an explicitly supported backend and registers the resulting executable with the existing engine configuration. It must work without reinstalling Suite or resetting settings, keys, databases or model assets.
- **Build and update:** pin a qualified upstream revision, record source/build/backend identities, build in a staging directory and verify the executable before atomic activation. Detect the GPU/toolchain and effective multi-GPU split capabilities, and explain missing requirements; do not silently substitute a CPU backend or advertise unsupported split modes. Preserve the previous working binary/configuration for rollback. Use existing queue/maintenance checks before replacing an active runtime; installation adds no independently scheduled daemon or public listener.
- **Optional dependencies and failure:** keep native build/runtime and selected preparation dependencies outside the base Suite dependency set. Model weights remain a separate tracked Files/download operation. Skipping leaves the rest of Suite usable; image capabilities report a clear “sdcpp not installed” state and actionable setup directions. A failed optional install returns an error and an explicit summary of what succeeded/failed, retaining any previous working installation and allowing a later retry.
- **Manual installation:** add a dedicated section to the maintained [installation guide](installation.md), covering supported platforms/backends and versions, build tools/drivers/toolkit checks, obtaining the pinned source, exact CMake/build commands, executable placement/configuration and permissions, version/capability verification, a small opt-in smoke generation, update/rollback and troubleshooting. Explain component formats/conversion and model acquisition separately. Include initial-installer and later-script examples and link this section from the feature's unavailable/setup state.

Generation and reference/output upscaling use sdcpp; UltraSharp native enlargement has passed local qualification. If a Python helper remains necessary, install its isolated dependencies only with the optional image feature and account for its GPU/RAM usage within the same admitted workflow. Do not assume sdcpp's upscaler accepts the existing Spandrel model files or produces equivalent preparation results.

### Automatic reference preparation

Pipeline: validate/decode -> normalize orientation/color -> derive target size -> model-upscale if small or downsample if large -> use the prepared reference for generation. Preserve aspect ratio without an automatic subject crop. Alpha uses a white matte; masks must match the first reference dimensions and preserve its output aspect ratio. References support aspect ratios from 1:8 through 8:1.

Reference preparation runs within LoadCoach admission and reports its stage/progress. Validate required upscaler/configuration before starting a job. Cache prepared derivatives only within the caller's permitted asset scope; key by original content hash, preprocessing version, target dimensions and upscaler file/configuration identity. A reference reused in a sweep should not be enhanced for each seed. No-log work uses temporary assets with cleanup, not a retained derivative cache.

Installed defaults within the confirmed reference-target policy (R1): 1 MP for klein and 0.5 MP for Dev on this machine, native 4x-UltraSharp, aspect preservation and 512-pixel native tiles. Do not run face reconstruction, crop a face or apply a whole-image diffusion pass merely because an input is small. Required-upscaler failure behavior is fixed by R2.

### Selected native runtime and comparison evidence

R13 records the selected runtime. Upstream documentation reviewed for the comparison describes the capabilities to qualify for Suite:

- [Supported backends/formats](https://github.com/leejet/stable-diffusion.cpp): native binaries, CPU/CUDA/Vulkan/Metal and additional backends, GGUF/safetensors, LoRAs and ESRGAN upscaling. This could reduce Python/PyTorch dependency packaging for native generation; retaining Spandrel preparation would still require Python image dependencies.
- [Flux 2 guide](https://github.com/leejet/stable-diffusion.cpp/blob/master/docs/flux2.md): klein 4B/9B generation and reference editing, including GGUF component choices. Exact igen model/adapter/reference equivalence is unqualified.
- [Device placement](https://github.com/leejet/stable-diffusion.cpp/blob/master/docs/backend.md): separate component/runtime/parameter placement and layer/row splitting for supported diffusion/text-encoder modules. Managed VRAM budgets exclude some allocations and do not replace LoadCoach admission/accounting.
- [Server API](https://github.com/leejet/stable-diffusion.cpp/blob/master/examples/server/api.md): image generation/edit compatibility endpoints and native job/progress/cancellation APIs. Its internal execution queue must remain subordinate to LoadCoach; do not add an independent Suite scheduler.

The [local comparison report](../data/qualification/image-runtime-comparison-20261023/report.html) contains timing/memory tables and paired visual outputs; [summary.json](../data/qualification/image-runtime-comparison-20261023/summary.json) is the canonical numerical result. All measured cases completed. Native generation was faster without references and used substantially less host RAM; igen was faster with two references. A second card modestly improved igen speed and reduced its steady host RSS, while loading still required a much larger RAM peak. Native speed barely changed with component separation. These results retain their scope and quantization limits; owner visual acceptance remains required for supported workflows.

The development harness is [compare-image-runtimes.py](../scripts/compare-image-runtimes.py). Initial bounded protocol: the same existing klein 9B source components, 832x1216, six Euler steps, zero/one/two prepared synthetic references, one-/two-card component placement, one warm-up plus three measured images per case. Igen uses its current NF4 configuration; native uses GGML Q4_0, so this compares practical configurations rather than identical quantization. Cross-request conditioning caches are disabled; model residency remains enabled. LoRAs, tensor splitting and human identity preservation are not established by this initial run.

Record process initialization, process-cold time to first image (OS caches retained), repeated image wall time, driver-level per-card/simultaneous total memory and process-tree RSS. Sample memory every 100 ms and retain sample gaps/raw telemetry, allocator peaks where available, source/binary hashes, logs and visual outputs. Reference preparation is metered separately and its exact results reused by both runtimes. The comparison uses Suite's existing external-work lock, refuses unknown/active benchmark queues or foreign GPU compute, yields to waiting foreground calls and stops at RAM/GPU safety headroom. It adds no production service, scheduler or benchmark score.

The retained run includes 36 timed images and 12 warm-ups on two RTX 5060 Ti 16 GB cards. [verification.json](../data/qualification/image-runtime-comparison-20261023/verification.json) records artifact checks, telemetry consistency, scoped tests and observed process/lock cleanup. Sampled driver peaks and process-tree RSS are evidence for designing admission, not complete qualified service/cgroup budgets. That initial run had CPU cancellation coverage; completed GPU cancellation/crash qualification is linked in Execution status.

Capacity qualification now includes actual diffusion layer splitting: Dev's 19.3 GB Q4_K_S diffusion component, staged Mistral encoder/VAE and complete reference/upscale pipeline passed on two 16 GB cards. Fixed unequal klein layer placement also preserved an entire text model on the second GPU. These results qualify the recorded profiles; row splitting and other model families remain unqualified. Runtime diagnostics reject silent split fallbacks. The completed cases and resource envelopes are recorded below.

Native commit and component/binary hashes are in the run manifest. Native could not use this model's Diffusers tensor names directly: [export-igen-flux2.py](../scripts/export-igen-flux2.py) restored BFL layout, checked every input tensor through the existing forward converter bit-for-bit, then read back every output tensor. Native transformer/encoder GGUFs were prepared and hash-verified before inference timing; originals remain intact. Conversion provenance is in `data/qualification/image-runtime-inputs/{klein-turbo-bfl,gguf}/conversion.json`. The archived `harness.py` matches the manifest; `reporter.py` records subsequent reporting/guard improvements without changing the measured rows. Future runs additionally save each pre-launch GPU baseline separately; the retained run's consistency check explicitly labels reconstructed baselines as inferred.

To repeat with the installed igen environment, native CUDA build and prepared inputs, use a new output directory (existing outputs are refused):

```bash
rtk proxy .venv/bin/python scripts/compare-image-runtimes.py \
  --igen-root /home/jpk/ai/igen \
  --igen-python /home/jpk/ai/igen/venv/bin/python \
  --native-source /home/jpk/ai/tools/stable-diffusion.cpp-image-compare \
  --native-server /home/jpk/ai/tools/stable-diffusion.cpp-image-compare/build-cuda/bin/sd-server \
  --native-transformer data/qualification/image-runtime-inputs/gguf/transformer-q4_0.gguf \
  --native-encoder data/qualification/image-runtime-inputs/gguf/encoder-q4_0.gguf \
  --suite-url http://10.77.10.42:8442 \
  --suite-key-file /home/jpk/ai/igen/.llm-api-key \
  --benchmark-host 10.77.10.42 \
  --benchmark-database /home/jpk/weightroom/data/benchmarks.db \
  --output data/qualification/image-runtime-comparison-NEW \
  --runtimes sdcpp igen
```

The existing key stays private; it is never copied into evidence. Defaults reproduce the bounded protocol above. `--layouts 1`, `--reference-counts 0 2`, `--repeats 3` and `--runtimes igen` can narrow a repeat. Failed/interrupted profiles remain visible in results rather than becoming successful timings; inspect the final summary status. The harness freezes inputs before loading models and refuses queued/running control node benchmarks, so allow existing work to drain first.

### API and configuration

Support `/v1/images/generations`, then supported reference/edit operations at `/v1/images/edits`, using existing API keys/model permissions. Publish explicit generation/edit/input-image capabilities so image workflows cannot accidentally satisfy chat routes. Preserve OpenAI request/response shapes where supported, return base64 images initially, and report unsupported settings instead of ignoring them.

Keep physical model/component definitions separate from versioned workflow presets. The request's `model` selects a configured execution profile; the UI can apply a private/shared preset bound to its stable model identity. Presets contain validated allowed settings, without prompts or private inputs. Native calls remain implementation code. Supported API overrides, limits, composite masks and streaming are documented in the operating guide; Responses-tool integration is outside this release.

### Chat image-generation window

Add **Image generation** within Chat using the existing WeightRoom navigation and UI components. Recommend settings/reference controls beside the output viewer on desktop, stacked on narrow screens. Model installation and workflow administration belong on Models; this window selects configured workflows/models and adjusts permitted settings for a run.

- **Editable settings:** derive fields, defaults and limits from the selected workflow/model. Proposed controls include prompt, supported negative prompt, output dimensions/aspect, seed/random seed, steps, sampler/scheduler, guidance, supported reference strength, LoRA strengths, image count and enabled upscaling. Group less-used controls under Advanced, including qualified component/split profile selection. Validate submitted values on the server and explain unsupported combinations rather than silently ignoring fields.
- **Reference uploads:** provide file selection and drag/drop, with previews, remove/reorder controls and workflow-defined slot/count limits. Preserve the uploaded original and automatically prepare the required derivative under R1–R2. Show preparation state and clear failures within the run; the user need not manually resize or upscale inputs before generating.
- **Run controls:** provide Generate and Stop with queued/preparing/loading/generating/upscaling/completed states as applicable. Keep entered settings and existing results visible on failure. Show completed images as they become available; partial-image streaming remains a separate R11 scope decision. All submissions and cancellation use the existing front/admission path and caller permissions.
- **Simple output browser:** show a thumbnail strip or grid and a larger view of the selected output, preserving aspect ratio. Clicking a thumbnail selects it; provide previous/next navigation, keyboard access, fit/original-size viewing and download. Keep selection stable when another image completes. Generate bounded thumbnail derivatives and load full-size images on demand.
- **Result details:** expose the selected image's resolved seed and generation settings in a compact details area so a result can be reproduced or its settings reused. Apply R10 to persistence and exported metadata; viewing details does not automatically embed prompts/references in downloaded PNGs.

The browser displays only the caller's authorized outputs; reference images, full-size results and thumbnail derivatives share the same access/retention rules. No-log runs use temporary assets and leave no retained gallery or thumbnail cache. Cross-session history/library persistence remains R10's decision. If the optional runtime is unavailable, show the established setup guidance in this window while preserving the form.

### Files and discovery

Extend existing Files/Hugging Face/download machinery beyond GGUF. Initial model assets include checkpoint/component shards, adapters, encoders, VAEs, tokenizers, scheduler/config files and upscalers. Shared dependencies must be tracked once and protected by the global usage union across configured nodes. Install verified complete bundles atomically and preserve storage originals.

Civitai discovery needs model-family/type/version/format filters, source IDs and hashes, trigger words, preview/content settings and license metadata. Use existing private credential conventions for a Civitai token. A downloaded model is not declared runnable until required components, format and workflow compatibility validate. Conversions need temporary disk/RAM budgets and verified derived-file provenance.

### Results and operations

Use existing control node ownership for retained user assets/history and worker ownership for model files/scratch. Decide reference-library persistence, output retention, intermediate retention and exported metadata before choosing schemas. Preserve no-log behavior throughout uploads, derivatives and outputs.

Report image count, sizes, elapsed/native timings and per-stage per-card/RAM peaks; do not fabricate language-token usage. Define cancellation, disconnect, timeout, restart recovery and batch partial-failure behavior. Surface models, workflows and their dependencies in the requested Models pages, with normal Logs/Status/Files integration.

## Interview decisions

Confirmed rows record owner answers from 2026-10-23. Implementation defaults use the authorized scope and remain editable per workflow. Keep decision details here rather than repeating them in status documents.

| ID | Decision | Owner answer / proposed choice | State |
|---|---|---|---|
| R1 | Reference target policy | Configurable per workflow. | Confirmed |
| R2 | Required upscaler missing/fails | Fail clearly; do not silently fall back to ordinary enlargement. | Confirmed |
| R3 | Initial post-processing scope | Upscaling only. Exclude igen's face refinement, restoration, hires and other repair/post-processing features; any future replacements must be designed from scratch rather than ported. | Confirmed |
| R4 | Retained versus omitted igen tools | Initial Flux2Dev and tested klein workflows, references, LoRAs, necessary converters and upscaling; omit unrelated igen application tools. | Confirmed |
| R5 | Workflow authoring and published API names | Editable presets initially; separate workflow/model definitions bound by a published preset. | Confirmed |
| R6 | Reference slots and reusable library | Ordered reference slots; private immutable originals and owner-scoped prepared derivatives. | Implementation default |
| R7 | Output sizes and defaults | Distinguish base diffusion size from final output size; configure allowed native-generation/upscale combinations per workflow. | Implementation default |
| R8 | One-/two-card policy and residency | Explicit qualified component/split profiles; uneven splits are acceptable and other models may remain resident when measured whole-workflow peaks plus headroom fit. Evict only eligible idle models when needed; preserve holds/locks and active work. | Confirmed |
| R9 | User run surface | Image-generation window under Chat with editable image settings, reference uploads and an output browser with thumbnails and a larger view; detailed contract above. | Confirmed |
| R10 | Storage/privacy | Private per-user reference/output assets on the control node, retained until user deletion with download/delete controls. No-log assets remain temporary. Keep sensitive exported metadata opt-in. | Confirmed; bounded implementation defaults for intermediates/limits |
| R11 | Editing scope | Include live partial-image previews in the first release; masks are a final-phase item. | Confirmed |
| R12 | Discovery policy | Adult Civitai previews require global permission plus each user's opt-in; globally off hides the user control and enforces safe previews. Private administrator-configured download credentials. Preserve existing assets on import. | Confirmed |
| R13 | Initial generation runtime | Use stable-diffusion.cpp (sdcpp), selected after the local automated comparison. | Confirmed |
| R14 | Installation | Optional sdcpp support offered during initial installation, a standalone script to install later and manual installation directions; detailed planned contract above. | Confirmed |
| R15 | Reference/output upscaler runtime | sdcpp-native 4x-UltraSharp passed reference/output enlargement; no separate Python model runtime is needed. Preserve R1–R3. | Implementation default |
| R16 | Larger-model capacity across cards | True diffusion-model splitting is required for larger models, alongside component placement; qualify supported two-card layouts and account for the whole workflow. | Confirmed |
| R17 | Advanced batch controls | Ordinary queued multi-image jobs initially; parameter sweeps and pause/resume in the final phase alongside masks. | Confirmed |
| R18 | Local installation and qualification | Download a fitting Flux2Dev quantization for this machine; stage text encoding before diffusion if needed to maximize available diffusion VRAM. Install required components, commit and deploy in stages until the plan is complete. | Confirmed |
| R19 | Feature ownership/layout | Mirror Wall owns the majority of image functionality in the root `mirrorwall/` package; preserve existing Suite scheduling, identity and storage authority boundaries. | Confirmed |
| R20 | Multi-model coexistence | A diffusion model may put most weights on one card while another model remains entirely resident on another; qualify reproducible split/peak envelopes instead of requiring equal utilization or unconditional exclusive reservations. | Confirmed |

## Implemented delivery and acceptance contract

1. Resolve interview scope; define model/workflow/asset identities and bounded API capabilities.
2. Add the optional native installer/shared later-install script and manual directions. Verify initial accept/decline, unattended choices, control-node-only behavior, dry-run/check, compatible existing/manual installs, missing toolchains, failed builds, retry and rollback. Skipping must leave ordinary Suite installation operational.
3. Integrate the sdcpp engine adapter and required igen helper behavior; import model assets non-destructively and validate native-compatible bundles/converters and automatic reference preparation.
4. Integrate admission, supervised execution, resource reporting and cleanup; qualify single-card, two-card component-placement and true diffusion-split workflows, including representative larger-model capacity, without interrupting existing queued/running work.
5. Add API routes, Models/workflow pages, Files/discovery and the Chat image-generation window using existing Suite components, including optional-runtime availability/setup guidance and explicit qualified component/split layout selection.
6. Qualify fixed-seed generation, automatic small/large/already-prepared references, aspect/orientation handling, permitted reference counts and deterministic derivative reuse. Include missing-upscaler failures, all enabled post stages and exact output dimensions.
7. Verify Chat setting changes reach the run accurately, unsupported fields fail clearly, reference upload/ordering/preparation works, and completed outputs populate thumbnails and the larger view. Check stable selection, previous/next, downloads, keyboard/narrow-screen operation, errors/cancellation and unavailable-runtime guidance; enforce access/no-log behavior for thumbnails as well as originals.
8. Verify LLM/image queue coexistence, GPU/RAM peaks, holds/locks, cancellation at preparation/loading/sampling boundaries, OOM/crash cleanup, restart/partial-result behavior and dependency-safe file cleanup. Verify per-user permissions, reference/result access and no-log cleanup.
9. Update maintained operating/install/storage contracts, package dependencies and deploy after affected work drains; distinguish local tests, deployed behavior and owner visual acceptance.
10. Add and qualify the final phase: parameter/seed sweeps, pause/resume at safe image boundaries with restart recovery, and masks for supported workflows. Preserve completed outputs and reject incompatible model/edit combinations clearly.

## Execution status

### Canvas studio and creative tools — 2026-11-13

The selected generation design is **01 Canvas studio**. Its live page and the design
previews share the inspector, prompt, viewer, sweep editor, result/reference/job templates
and palette-driven surfaces. Keep work in Mirror Wall and use existing control node assets,
model permissions, admission and batch execution; do not add another job service.

| Step | Deliverable and acceptance | State |
|---|---|---|
| 1 | Canvas studio: real model/workflow selection, per-model drafts, family-aware fields, ordered uploads/drop, composite masks, LoRAs, sweeps, queue pause/resume/stop, library, comparison, settings reuse/export, keyboard and narrow layouts. Polls preserve edits and selection; unavailable models explain the next action. | Implemented; validation below |
| 2 | **Resize & add detail**: upload or select an owned image, choose 2×/4×/custom dimensions or same-size refinement, lock source proportions, adjust detail strength and optional instructions. Run an exclusive SDXL image-to-image detail pass; use the configured neural upscaler for required source/final enlargement and finish at the exact requested dimensions. Never substitute plain enlargement if the AI upscaler is missing or fails. | Implemented; native visual qualification pending |
| 3 | Painted composite masks: brush/erase, zoom, undo/redo, invert and feather; export a source-sized PNG through the current mask API. Follow with native inpainting as a separate capability and compatible model profile. Verify protected pixels, alpha, EXIF orientation and cancellation. | Next |
| 4 | Expand canvas: anchors, target aspect/size, overlap and mask preview; requires qualified native inpainting and tiled work beyond the normal pixel envelope. Preserve the original and show how its pixels will be placed. | After 3 |
| 5 | Qwen instruction editing: explicit Edit/2.1 vision profile, compatible vision projection, ordered inputs and instruction controls. Update catalogue capabilities, component validation and resource measurements; keep current text-only profiles unchanged. | Planned |
| 6 | Structure/pose and identity/style: ControlNet depth/edges/pose plus supported image adapters; declare exact family compatibility, configured assets, strengths and start/end timing. Qualify each combination before offering it. | Planned |
| 7 | Remove/isolate: segmentation, editable selection, transparent PNG output and background replacement. Requires its own bounded model capability, alpha-preserving asset/export contracts and private derivatives. | Planned |
| 8 | Refine/restore: tiled generative enlargement, seam blending and optional face restoration. Keep restoration opt-in and separate from the implemented SDXL detail resize. Measure tile overlap, identity drift, memory peaks, recovery and full-image output dimensions. | Planned |
| 9 | Prompt assistance: saved fragments first; then optional caption/rewrite calls using authorized language/vision models. Show proposed text for review, preserve the original draft and never send private images to a cloud service implicitly. | Planned |

**Resize contract.** `detail_resize: {width, height}` extends the ordinary image edit
request. It requires one owned source, one output, an exclusive SDXL workflow, no mask
and `upscale=1`; the explicit target replaces factor-based output enlargement. Final
dimensions are 16–8192 pixels, at most 32 megapixels, and at most the configured
upscale limit (never above 4× the detail-pass dimensions). The working diffusion size
still obeys the selected workflow’s dimensions, alignment and pixel budget. Source
preparation uses that working size. Low strength preserves more of the input; generated
detail can alter fine features and is not evidence of details in the original. Preserve
the immutable source, per-user ownership, temporary/no-log policy and clean PNG exports.
Existing resource-sharing qualifications do not qualify the new operation.

**Completion checks.** Exercise authenticated routes and real stored assets, exact final
sizes, parameter forwarding, source immutability, unsupported-family/profile rejection,
upscaler failure, temporary inputs, cross-user refusal, cancellation and retained outputs.
Run affected image tests plus the UI component audit and all-page browser checks after
shared component changes. Before production qualification, measure native SDXL detail
resize and AI-upscaler stages on a compatible installed profile, including same-size,
2×, custom aspect, output limit and cancellation cases; owner reviews source fidelity
and invented detail. This change does not install weights or restart services. Any later
deployment uses `owg update; owg restart` and verifies the effective source and behavior.

**Validation (2026-11-13):** 84 affected checks passed, including authenticated browser
flows at 1600/390/320px and native-workflow contract tests with a fake inference engine.
The broader run had 124 passes and eight unrelated failures: six Backup raw-`None`
checks, Status copy, and the legacy Civitai policy route returning 404. The image page
passed all audited appearances/widths. Native GPU visual qualification and deployment
have not been performed.

The canonical machine record is [delivery.json](../data/qualification/mirrorwall-20261023/delivery.json); large logs and row-level results stay under ignored `data/qualification/`.

| Area | Result / evidence |
|---|---|
| Installation/deployment | Optional pinned CUDA runtime installed; control node API/UI, artifact worker and main GPU worker activated through immutable source pins. Previous releases, private configuration preimages and source-only control node recovery copy are preserved. See [source override recovery](operations.md#runtime-source-overrides). |
| Workflows | Live front-generated Dev 1024px/28-step and klein gallery outputs; maximum configured 1024px/two-reference/4× pipelines and six native previews passed for Dev, single-card klein and unequal layer-split klein with blueprint LoRA. [Cases](../data/qualification/mirrorwall-20261023/live-workflows-flux2-klein-split-q4.json). |
| Resource admission | [Per-card envelopes](../data/qualification/mirrorwall-20261023/live-profiles.json) include preparation, loading/sampling and output upscaling; incomplete/failed observations revoke sharing, successful smaller runs retain a current qualified envelope, and unqualified profiles retry on whole cards. Dev uses 0.5MP references because two 1MP references exceeded this machine's native budget. |
| Coexistence | Fixed 3:1 diffusion placement generated while Qwen 1.5B remained entirely resident on CUDA1 with its process ID preserved. [Evidence](../data/qualification/mirrorwall-20261023/live-coexistence.json). |
| Controls/recovery | [Live masks, sweeps and queue controls](../data/qualification/mirrorwall-20261023/live-controls.json): protected pixels retained; four-image sweep paused, survived a control node API restart and resumed without duplicates; Stop cancelled eight queued images. [Cleanup](../data/qualification/mirrorwall-20261023/live-cleanup.json): preparation/loading/sampling cancellation and native crash released reservations and GPU children. |
| Privacy/export | Stable per-user ownership covers originals, thumbnails and results; unauthorized reads return 404. Authorized temporary output had no control node SQL row or disk file. [Evidence](../data/qualification/mirrorwall-20261023/live-privacy.json). Render exports explicitly strip inherited metadata; masks require matching source dimensions and output aspect ratio. |
| UI | Private preset/version, exact seed reuse, thumbnail/keyboard browsing, clean PNG download and 390px single-column layout checked. [Live studio](../data/qualification/mirrorwall-20261023/live-studio.jpg). Browser filechooser automation lacked extension file-URL permission; API uploads and reference/masked generation passed. Human file selection/visual quality remain owner review. |
| Discovery/Files | Image components/dependencies are tracked across nodes. Civitai filters, global-off control hiding and per-user opt-in passed; [preview policy](README.md#images). An authenticated 83 MiB LoRA download through the existing manager passed SHA-256 verification into the control node's configured storage library: [transfer](../data/qualification/mirrorwall-20261023/civitai-download.json). Real HF blueprint LoRA loaded all 224 tensors. |
| Regression | Frozen image-runtime suite: **1,648 passed, 38 skipped**. Subsequent preview-policy/settings/routing checks: **71 passed**. [Full JUnit](../data/qualification/mirrorwall-20261023/suite-final.xml), [policy JUnit](../data/qualification/mirrorwall-20261023/preview-policy.xml). |

The source igen installation and its user data remain intact. Visual quality acceptance belongs to the owner; these qualifications cover the recorded CUDA profiles, not every model/backend or a clean Suite reinstall.

### Review 2026-10-24

Operator guide: Docs → Modules → Mirror Wall (`weightroom/pages/docs/modules/mirrorwall.html`): the workflow diagram, a models table with each model's parts, sizes and measured peaks, adding a model, and reference-image rules.

- Fixed: an SDXL checkpoint was launched with `--diffusion-model`, which loads its tensors under the diffusion prefix, so its built-in CLIP and VAE could not load; a checkpoint without separate CLIP files now uses `--model`. SDXL uses only its first reference as the start image, yet accepted two: its limit is now one, and a profile asking for more is refused. SDXL strength had no control on the Images page.
- Fixed: a reference smaller than the reference size on a model without an upscaler, too many references and a mask without a reference all failed only after the job was queued; control node now refuses them at once, and the Images page says why before Generate.
- Added: the Images page marks, per model, whether references are used, how many and how (conditioning or image-to-image), and each reference as main, enlarged, too small or not used. Models → Images reads each file's GGUF or safetensors header on the selected node (`mirrorwall/components.py`, `POST /loadcoach/images/check`, files outside the node's model places are not read), detects the family and size, marks each part required, optional or unused, names a wrong part (such as a Qwen3 4B encoder for klein 9B), offers matching files already on the node and links the Hugging Face repository for a missing one.
- Fixed during integration: model-part searches enforce allowed roots through symlinks, malformed tensor shapes report unreadable files, and invalid reference megapixels are refused before dimension calculations. The intermittent preview-policy test shared clients across two event loops; it now uses the app's existing loop and passed ten consecutive checks.
- SDXL Base 1.0 passed native 512×512 generation through 20 steps, fixed-seed negative-prompt comparison, cancellation and cleanup on 2026-10-27; [profile and receipts](../data/qualification/model-image-settings-20261027/sdxl.json). Flux 2 klein 4B remains unqualified; owner visual acceptance remains separate.

### Combined deployment 2026-10-24

Claude's `615ce293`/`59b85901`, SoundRoom usability work and integration guards are combined on `main`; runtime source is `395fb7ac`, exported to `~/.local/share/ai-suite/releases/unified-395fb7ac/source` on compute, control node and remote CPU. All 12 previously active core/adapter services select it through `99-unified-20261024.conf`; the separate control node ToolLocker host's shipped files and the Hermes instance's runtime bundle were also updated. Installed source/CLI copies match. Settings, keys, native engines, limits and inactive services were preserved.

Validation: the complete suite passed **1,709 tests, 38 skipped** (one existing Starlette/httpx deprecation warning). Live authenticated pages, matching JS hashes, all three image profiles' component checks, signed management calls to all workers, anonymous API refusal and standalone tool discovery passed; the browser showed the operator guide and model checklist without console errors. The benchmark queue was paused only while idle, then its original pause state and retained run/batch counts were restored; no active voice or adapter run was interrupted. [Machine-readable receipt and test results](../data/qualification/unified-20261024/report.json).

Recovery: after work drains, remove only the new `99-unified-20261024.conf` from the affected user-service drop-in directories, reload systemd and restart those services against their retained compatible source overrides. Restore the standalone shipped files from `unified-395fb7ac/tool-host-before/` and Hermes runtime files from `unified-395fb7ac/adapter-before/` before their restarts; preserve instance settings, keys and data. If an entry point imports from its working directory, restore that installed package copy from the selected prior release too. Do not roll back databases.

