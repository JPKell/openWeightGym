# Advanced image workflows plan

Status: 2026-11-17 — phases 0–4 delivered: the live Stage lanes editor at Models → Image
workflows runs test runs, and published workflows answer `workflow:<name>` calls and
`GET /v1/workflows` ([mirrorwall/graph_run.py](../mirrorwall/graph_run.py)). Phase 5 (control
images and tiled detail) is delivered as well; Phase 6 (video graphs) is next. The owner's
interview answers are recorded in [Decisions](#decisions-interview-2026-11-17) and are reflected
throughout.

Phase 5 as delivered differs from the plan below in these ways:

- A ControlNet is an optional `control_net` component of an SDXL model entry, loaded with
  `--control-net` for the native server's life. It is allowed only on exclusive entries, because
  shared-residency profiles were not measured with it. One entry loads one ControlNet.
- The runtime finds edges itself (`image_preprocess` `canny=true`); depth and pose images are used
  as given, since this app has no depth or pose estimator. FLUX control images (`control_image`
  without a ControlNet) need a Flux 1 Canny/Depth family, which the app does not run.
- Tiled detail runs on the control node. Each tile is one image request, and the tiles are blended
  with linear fades across the overlaps. A step makes at most 64 tiles. An SDXL start image may
  now be prepared at the output size beyond the model's reference size, so 1024-pixel tiles and
  refine passes work.

Phase 4 as delivered differs from the plan below in these ways:

- Intermediate images stay in the run's memory rather than becoming 24-hour assets; Outputs are
  saved with the workflow version and every step's settings. Stage requests carry
  `x-mirrorwall-intermediate: 1`, so the image API returns them without saving.
- Runs can be stopped, not paused or resumed. Test runs are background jobs the page polls,
  because the in-process hop from the web app to turnstile buffers streams.
- A hires pass naming an upscaler file is a run problem: `sd-server` reads hires upscalers from
  one folder given at start, which a per-request choice would have to restart. Upscale steps run
  any installed file through an upscale-only request (`upscale_with`), resolved by each node's
  LoadCoach from its own file index and passed to the runtime in a private header.
- Upscale steps pin the node that made their image when it holds the file (the request body's
  `node`, so Settings → General must allow callers to choose the node); otherwise routing chooses.
- Image generation does not list workflows yet.

## Goal

Let authorized users set up multi-step image (and later video) workflows on a visual canvas,
similar in power to ComfyUI but without hand-wiring every input to every output. Then let
people and API clients call those workflows like a model:

- A visual graph of components (prompts, models, samplers, refine passes, upscalers, masks, outputs).
- Connections chosen from the source: each output lists the compatible destinations, and the
  user ticks one or more. Wires are drawn automatically; dragging wires is optional.
- Several components of one kind in a graph: several samplers, models, LoRA stacks and outputs.
- Every native sampler, scheduler and advanced option the installed engine supports, gated
  by what each model family actually accepts.
- Saved workflows callable through the existing image API (`model="workflow:<name>"`), with a
  separate workflow listing and a test-run panel that shows a ready-made API example.
- Shipped sample workflows with model-appropriate defaults.
- A guide explaining advanced workflow concepts.

## What exists today

| Area | Today | Source |
|---|---|---|
| Workflows | One flat, versioned settings preset per model: size, steps, sampler, scheduler, guidance/CFG, reference size, strength, negative prompt, LoRA strengths, sharing | [mirrorwall_workflow_editor.html](../mirrorwall/pages/mirrorwall_workflow_editor.html), `mirrorwall_presets` / `mirrorwall_preset_versions` in [schema.py](../mirrorwall/schema.py) |
| Job pipeline | Control node expands a request into batch lines (count × sweep, at most 128 images), admits one model per turn and streams each line through `/v1/images/generations` or `/edits` | `lines`, `start`, `execute_line` in [front.py](../mirrorwall/front.py) |
| Node runtime | One `sd-server` per model; `workflow()` prepares references (AI upscale when small), calls `img_gen`, composites masks in Python, then upscales or applies the SDXL detail resize | [runtime.py](../mirrorwall/runtime.py) |
| Native engine | Pinned stable-diffusion.cpp `95ff762`. `img_gen` accepts `init_image`/`strength`, `mask_image`, `ref_images`, `control_image`/`control_strength`, `hires` (latent or model upscaler, steps, denoise, custom sigmas, tile size), `vae_tiling_params`, `cache_mode`, `clip_skip` and full `sample_params` (eta, flow shift, shifted timestep, img CFG, skip-layer guidance, custom sigmas). `vid_gen` adds `high_noise_sample_params`, `moe_boundary`, VACE and end frames. `/sdcpp/v1/capabilities` reports features per loaded model | [server API](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/examples/server/api.md) |
| Samplers offered | `euler, euler_a, heun, dpm2, dpmpp2s_a, dpmpp2m, dpmpp2mv2, lcm, ipndm, ipndm_v` | `SAMPLERS` in [model.py](../mirrorwall/model.py) |
| Native samplers | `euler, euler_a, heun, dpm2, dpm++2s_a, dpm++2m, dpm++2mv2, ipndm, ipndm_v, lcm, ddim_trailing, tcd, res_multistep, res_2s, er_sde, euler_cfg_pp, euler_a_cfg_pp, euler_ge, dpm++2m_sde, dpm++2m_sde_bt, lms` | `src/stable-diffusion.cpp` at the pinned revision |
| Native schedulers | `discrete, karras, exponential, ays, gits, sgm_uniform, simple, smoothstep, kl_optimal, lcm, bong_tangent, ltx2, logit_normal, flux2, flux, beta, llada_image` | same |

### Defect to fix first

The image runtime sends sampler names such as `dpmpp2m` to `sd-server` unchanged
(`runtime.py`, `sample_method: resolved["sampler"]`). The native names are `dpm++2m` etc.,
and the server silently keeps its default for an unknown name
(`examples/common/common.cpp`, `str_to_sample_method` → `SAMPLE_METHOD_COUNT` is ignored).
Image jobs asking for any DPM++ sampler therefore run the family default instead. The video
path already maps `dpmpp` → `dpm++`. Phase 0 fixes this, and every sampler/scheduler name
is validated against the loaded model's `/sdcpp/v1/capabilities` rather than a fixed list.

## Key design constraint: images, not latents, flow between samplers

ComfyUI passes latents between nodes. `sd-server` takes and returns images per request, so
a graph here hands **images, masks, text and settings** between steps. Consequences:

- Each Sampler (and each Upscale/Detail step) is one native request, called a **stage**.
- A second sampler fed by the first is an image-to-image pass. Its **strength** (denoise)
  controls how much it changes. A VAE decode/encode round trip happens in between; the guide
  explains why this is usually invisible at strength ≤ 0.5.
- Two native features keep work in latent space inside **one** stage, and the graph exposes
  them as options of a single Sampler, not as separate nodes:
  - **Hires pass** (`hires`: latent or model upscaler, scale/target, steps, denoise).
  - **Two-expert video sampling** (`high_noise_sample_params`, `moe_boundary`) for Wan 2.2 A14B.
- No new native patch is required for Phases 0–4. Latent hand-off between stages stays out
  of scope unless qualification shows a visible quality gap. That would need upstream support.

## Concepts and data model

### Graph document

A workflow is a versioned JSON document stored in new tables
(`mirrorwall_graphs`, `mirrorwall_graph_versions`; migration 4 in `schema.py`).
It mirrors the preset tables' ownership, publishing and history rules.

```json
{
  "schema": 1,
  "name": "SDXL base → hires → upscale",
  "media": "image",
  "nodes": [
    {"id": "prompt", "type": "prompt", "title": "Prompt", "values": {"text": ""}, "exposed": ["text"], "at": [40, 60]},
    {"id": "model", "type": "model", "values": {"model": "sdxl-base", "loras": {}}, "at": [40, 260]},
    {"id": "base", "type": "sampler", "values": {"steps": 28, "sampler": "dpm++2m", "scheduler": "karras", "cfg_scale": 6.5, "width": 1024, "height": 1024, "seed": -1}, "at": [360, 140]},
    {"id": "out", "type": "output", "values": {"label": "Final"}, "at": [680, 140]}
  ],
  "links": [
    {"from": ["prompt", "text"], "to": ["base", "prompt"]},
    {"from": ["model", "model"], "to": ["base", "model"]},
    {"from": ["base", "image"], "to": ["out", "image"]}
  ]
}
```

- `exposed` lists the values a caller may set: in the test-run panel, in Image generation and
  through the API. A complex graph then behaves like a simple form (prompt, source image,
  seed, count). Sampler fields can be marked **overridable** with bounds (for example steps
  8–20 on the `refine` step); everything else is fixed by the author.
- Every node has a unique **label** (`base`, `refine`, `upscale`), used in API overrides and
  output names. Renaming a label is a new version and is flagged as an API change.
- Positions (`at`) are layout only; execution never depends on them.
- Bounds: 64 nodes, 128 links, 32 stages per run, the existing 128-image job cap, 24,000
  characters per prompt, documents ≤ 256 KiB.

### Port types

| Type | Carries | Fan-out | Inputs accept |
|---|---|---|---|
| `text` | Prompt or negative prompt | many destinations | one source (Combine text joins several) |
| `model` | An installed image model plus its LoRA stack and workflow limits | many | one |
| `image` | One image per run item | many | one; **Reference images** accepts an ordered list up to the model's limit |
| `mask` | Greyscale mask, white = change | many | one |
| `seed` | Integer or "random once per run" | many | one |
| `size` | Width × height | many | one |
| `video` | Clip plus frame data (Phase 6) | many | one |

The validator rejects type mismatches, cycles and inputs a model family cannot use
(for example `mask` into a Flux 2 sampler before native inpainting is qualified for it).

### Components

| Component | Inputs → outputs | Notes |
|---|---|---|
| **Prompt** / **Negative prompt** | → text | Multiple allowed; exposed by default |
| **Combine text** | text × n → text | Joins with a separator; used for style suffixes |
| **Model** | → model | Picks an installed model the user may use, plus LoRA strengths. A Model may feed many Samplers |
| **Seed** | → seed | Fixed, random once per run, or increment per item; shared seeds make comparisons fair |
| **Size** | → size | Presets by aspect ratio, aligned to the family (16 or 32 px) |
| **Load image** | → image | An owned library image or upload |
| **Mask** | image → mask | Painted with the existing composite-mask editor; invert, feather, grow |
| **Sampler** | model, text, negative, seed, size, image (init), images (references), mask, control image → image | One native `img_gen`. Its panel shows only the fields the model's capabilities allow: sampler, scheduler, steps, CFG, distilled guidance, img CFG, flow shift, eta, shifted timestep, skip-layer guidance, custom sigmas, strength, clip skip, VAE tiling, step cache, and the optional **Hires pass** sub-section |
| **Upscale** | image → image | Model upscaler (ESRGAN family) by factor or exact size; never a silent plain resize. Optional Lanczos finish to an exact size |
| **Resize / crop** | image → image | Plain geometry: fit, fill, crop, pad, align |
| **Detail faces / objects** | model, text, image → image | Native ADetailer pass with a configured YOLOv8 detector (Phase 7) |
| **Control image** (Phase 5) | image → image | Depth, edge or pose preprocessing for ControlNet, or a FLUX control image; feeds a Sampler's control input with strength and model |
| **Tiled detail** (Phase 5) | model, text, image → image | Splits a large image into overlapping tiles, runs a low-strength detail pass per tile and blends seams; bounded tile count |
| **Composite** | image, image, mask → image | The existing Python mask composite as its own step |
| **Compare** | images × n → board | Side-by-side or grid; saves each input plus an optional contact sheet |
| **Output** | image → saved image | Saves to the library with the full resolved settings of every stage that produced it. Multiple outputs allowed |
| **Note** | — | Text on the canvas; not executed |
| **Video sampler** (Phase 6) | model, text, image (start/end), video (control) → video | One native `vid_gen`, with a high-noise/low-noise section for two-expert models |
| **Last frame** (Phase 6) | video → image | Feeds clip extension chains |

Sweeps (seed, steps, CFG, guidance, size, strength, sampler, scheduler) attach to any
Sampler field and multiply run items, within the existing 128-image cap.

## Connecting without wiring

The canvas draws every link, but users don't have to drag wires.

1. **Send to** (source side). Selecting a node opens its inspector. Each output lists every
   compatible destination input on the graph, grouped by node. Each row has a checkbox, the
   node title and the input name. Ticking adds a link; several ticks fan the output out. Already
   bound inputs show their current source and offer **Replace**. Incompatible or
   cycle-creating destinations are listed disabled with the reason.
2. **Use from** (destination side). Each input shows a dropdown of compatible sources, so
   either end can make a link.
3. **Automatic binding.** A new node connects its required inputs to the only, or the nearest
   upstream, compatible source. For example, a new Sampler takes the existing Prompt, Model,
   Seed and Size. Each automatic link is labelled until the user confirms or changes it.
4. **Insert after.** "Add after" on a node inserts a component and re-routes the node's
   existing destinations through it. For example, Upscale after Base Sampler passes through to Output.
5. Optional wire dragging for pointer users, with the same validation.
6. An **Outline** view lists nodes in run order with the same Send to / Use from controls.
   It is the narrow-screen and keyboard/screen-reader equivalent of the canvas, not a separate
   editor.

Every link change is undoable (undo/redo stack in page memory) and validated immediately.
Drafts stay in page memory across workflow switches, as other studios do.

## Execution

1. **Compile** on the control node: validate types, bounds, permissions and each model's
   capabilities and limits. Order stages topologically, and expand sweeps and counts into run
   items. The client gets the same validation messages before Run is enabled.
2. **Group by model and place by normal routing.** Consecutive stages on one model run in one
   `image_turn`, so the native server stays loaded. Each stage uses existing model routing.
   When one node has every model the run needs, prefer keeping the whole run there, so
   intermediate images don't move between nodes. Otherwise stages may run on different nodes,
   and the control node hands images between them. A stage on another model releases and
   re-admits through existing admission. The run preview shows model loads and likely nodes.
3. **Stage requests** reuse the image request contract, extended with an allowlisted
   `advanced` object (native field names, validated and bounded). Results go back to the
   control node as private **intermediate assets** owned by the run's user, retained for
   24 hours unless an Output saves them. Temporary/no-log runs keep intermediates in memory
   and scratch only, as today.
4. **Progress** streams per stage and per run item: stage name, model, step/total, preview
   frame. The canvas highlights the running node; Outline shows the same state.
5. **Cancellation, pause and resume** reuse the batch line controls; completed stages are not
   repeated after resume (per-stage idempotency keys, like `batch_line` metadata today).
6. **Reproducibility.** Every saved image records the graph version, its run item, all
   resolved stage settings, seeds and model content hashes. **Open in editor** restores the
   exact graph and values.

Resource qualification still applies per profile. A graph adds no new sharing envelope: each
stage is admitted with its model's existing reservation.

## API

Workflows are set up on the Image workflows page and then called like a model.

- **Run**: `POST /v1/images/generations` (or `/v1/images/edits` when the workflow exposes
  an image or mask input) with `model: "workflow:<name>"`.
  - Standard fields map to exposed inputs: `prompt`, `negative_prompt`, `size`, `n`
    (run items), `seed`, `image`/`image[]`, `mask`. A field the workflow doesn't expose is
    refused with a clear message rather than ignored.
  - `workflow_inputs` sets any other exposed input by name (`{"style": "ink wash"}`) and
    per-step overrides by step label (`{"refine.steps": 14, "refine.strength": 0.3}`).
    Only fields the author marked overridable are accepted, within the author's bounds and
    the model's limits.
  - The response's `data[]` holds **every Output image**, labelled with its Output label,
    run item, seed and resolved stage settings. `n` multiplies run items. Streaming emits
    per-stage progress and partial images with the stage label.
  - `stream`, temporary/no-log behavior, request logging and usage accounting follow the
    existing image API. Each stage is recorded as its own request linked by one trace.
- **List**: `GET /v1/workflows` returns the workflows the caller may run, with exposed inputs,
  overridable fields and bounds, outputs, models and current version. `GET /v1/models` is
  unchanged.
- **Versions**: a call runs the workflow's current version. `workflow:<name>@<version>` pins
  a version for reproducible clients.
- **Access**: no separate permission. A caller may run a published workflow when they may use
  every model it uses (see [Access](#access-and-abilities)).

## Access and abilities

- New ability **`manage-workflows`** (in `loadcoach/groups.py` `ABILITIES`, with its generated
  docs entry): create, edit, version, publish and delete image workflows. It can be granted per
  group or user, independently of `manage-models`.
- **Running** follows the models: a published workflow is runnable by anyone allowed to use all
  of its models. It appears in Image generation and `/v1/workflows` for them. When a group
  loses access to one model, its workflows that use that model disappear for that group.
- Drafts and unpublished versions are visible only to `manage-workflows` holders.
- Deleting a workflow keeps saved outputs and job records; API calls then return "no such
  workflow".

## Pages

| Route | Purpose |
|---|---|
| `/models/image-workflows` (Models → Image workflows) | Library of published, draft and sample workflows: search, model family, duplicate, history, delete. Visible to `manage-workflows` holders |
| `/models/image-workflows/<name>` | Editor: canvas (or Outline), inspector with Send to / Use from, component palette, exposed/overridable field settings, and a **test-run panel** with exposed inputs, per-step previews and intermediates, plus generated curl and Python examples for the API call |
| `/chat/images` (existing) | Lists runnable workflows as a separate **Workflows** group in the picker, apart from models; choosing one shows only its exposed inputs |

Follow the repository process: build three standalone proposals with sample data first, then
the owner selects one for the live page. Suggested proposals:

- **01 Node canvas**: full-bleed canvas, floating palette, right inspector with Send to.
- **02 Stage lanes**: left-to-right lanes per stage with collapsible cards; links shown
  as chips, with a canvas toggle.
- **03 Split studio**: canvas above, run inputs and results below. Suited to iterating on a
  saved graph.

Proposals reuse `generation_design_parts.html` (prompt, viewer, library templates),
`image_design_parts.html` (model fields), `parts.html` (cards, form sections, confirmation,
`option_search`) and palette tokens. Canvas rendering is SVG for links and HTML for nodes,
redrawn on `weightroom:theme`. Add a new template index row in AGENTS.md for the shared
graph node, port row and Send to list templates.

## Sample workflows and defaults

Ship samples as read-only published graphs. **Duplicate** makes an editable copy, and a
sample is offered only when the user has a compatible installed model. Each sample's
values come from the family defaults below. Values marked *community* come from widely
used ComfyUI workflows and need native qualification before they become defaults.

### Family defaults for a Sampler

| Family | Steps | Sampler / scheduler | CFG / guidance | Other | Basis |
|---|---|---|---|---|---|
| Flux 2 klein 4B / 9B (distilled) | 4 | euler / flux2 | CFG 1 | ≤ 4 references | Upstream `docs/flux2.md` uses 4 steps at CFG 1. Current profile default is 6 and should be reviewed |
| Flux 2 klein base 4B / 9B | 20 (up to 50 for 9B) | euler / flux2 | CFG 4 | Negative prompt meaningful | Upstream `flux2.md`; ComfyUI blog lists 50 steps for 9B base |
| Flux 2 Dev | 28 | euler / flux2 | guidance 4, CFG 1 | References for editing | Current profile, upstream `flux2.md` |
| SDXL base | 28 | dpm++2m / karras *(community)* | CFG 6–7 | clip skip 1; VAE fp16-fix | Current profile, common practice |
| SDXL hires pass | +12–15 | same | same | scale 1.5, denoise 0.3–0.45, ESRGAN or Latent upscaler | Common 2-pass practice, upstream `hires` |
| Qwen Image | 50 | euler / discrete | CFG 4 (upstream example 2.5) | flow shift 3 | Current profile, upstream `qwen_image.md` |
| Qwen Image + Lightning 8-step LoRA *(community)* | 8 | euler / simple | CFG 1 | shift 3, LoRA 1.0 | Lightning LoRA guides |
| Qwen Image 2.1 | 50 | euler / flux | CFG 6 | 32-px alignment; turbo sigmas 8 steps at CFG 1 | Upstream `qwen_image_2.1.md` |
| MOSS-Image | 50 | euler / discrete | CFG 3 | default flow shift 5 | Upstream `moss_image.md` |
| Krea 2 Raw / Turbo | 20 / 8 | euler / discrete | CFG 3.5 / 1 | Turbo ignores negatives | Upstream `krea2.md` |
| Wan 2.1 (video) | 20 | euler | CFG 6 | flow shift 3, 33 frames | Upstream `wan.md` |
| Wan 2.2 A14B (video) | low + high noise | euler both | CFG 3.5 each | flow shift 3; high-noise steps per upstream example | Upstream `wan.md` |
| Wan 2.2 + Lightx2v 4-step LoRAs *(community)* | 2 high + 2 low | euler or lcm / simple | CFG 1 | LoRA on both experts | Lightx2v guides |
| LTX 2 (video) | per recipe | euler | CFG 3 | hires with the LTX latent upscaler, 4–6 hires steps | Upstream `ltx2.md` |

### Samples

| # | Sample | Shows | Graph |
|---|---|---|---|
| 1 | **Quick draft** (any family) | Exposed inputs, family defaults | Prompt, Model, Seed, Size → Sampler → Output |
| 2 | **SDXL two-pass hires** | Hires pass inside one Sampler | Sampler (28 steps; hires ×1.5, 14 steps, denoise 0.38) → Upscale 2× (ESRGAN) → Output |
| 3 | **SDXL two-model refine** | Two models, image-to-image hand-off | Base Sampler (SDXL checkpoint, 28 steps) → Refine Sampler (a second SDXL checkpoint, strength 0.25, 14 steps) → Output; one Prompt and Seed fan out to both. The pinned sd.cpp has no SDXL refiner support, so the second pass uses an ordinary SDXL checkpoint |
| 4 | **Sampler shoot-out** | Fan-out to several samplers, Compare | One Prompt + Seed + Model → 4 Samplers (euler/simple, dpm++2m/karras, res_multistep/beta, er_sde/kl_optimal) → Compare → Output each |
| 5 | **Family comparison** | Multiple models, shared seed | Prompt → Klein 4B, Krea 2 Turbo, Qwen Image Samplers → Compare |
| 6 | **Krea Turbo draft → SDXL detail** | Cheap composition, detailed finish | Krea Turbo Sampler (8 steps, CFG 1) → Upscale to 2048 → SDXL Sampler (strength 0.3) → Output |
| 7 | **Klein multi-reference edit** | Ordered reference list | Load image × 3 → References of a Klein Sampler (4 steps, CFG 1) with an instruction prompt → Output |
| 8a | **Structure-guided generation** (Phase 5) | ControlNet / control image | Load image → depth or edge control → Sampler (control strength 0.6–0.8) → Output; FLUX control images where the family declares `control_image` |
| 8b | **Tiled 4× upscale** (Phase 5) | Tiled detail pass | Sampler → Upscale 2× → tiled SDXL detail (strength 0.3, overlap 64 px, seam blending) → Upscale 2× → Output |
| 8 | **Inpaint and blend** (Phase 7) | Mask, native inpaint, Composite | Load image → Mask → SDXL Sampler (mask, strength 0.75) → Composite with the original via the feathered mask → Output |
| 9 | **Qwen Lightning fast** *(needs the LoRA)* | LoRA-driven step reduction | Model (Qwen Image + Lightning 8-step at 1.0) → Sampler (8, euler/simple, CFG 1, shift 3) → Output |
| 10 | **Portrait with face detail** (Phase 7) | Detector pass | SDXL Sampler → Detail faces (strength 0.4, dpm++2m/karras) → Output |
| 11 | **Wan 2.2 two-expert clip** (Phase 6) | High/low-noise section | Prompt + start image → Video sampler (high 3.5/low 3.5 CFG, shift 3) → Output |
| 12 | **Wan clip extension** (Phase 6) | Last frame chaining | Video sampler → Last frame → Video sampler → Output both |

Each sample carries a Note node explaining what to change first and why.

## Documentation

- **New guide** `docs/guide/models/image-workflows.md`, covering
  `/models/image-workflows*`, with sections:
  1. What a workflow graph is; components, ports and types.
  2. Connecting: Send to, Use from, automatic binding, Insert after, fan-out, Outline.
  3. Stages and why images (not latents) pass between samplers; what strength/denoise does.
  4. Samplers vs schedulers: deterministic vs ancestral/SDE samplers (`_a`, `sde`, `er_sde`),
     CFG++ variants, which schedulers suit flow-matching (flux, flux2, simple, beta) versus
     UNet models (karras, exponential, kl_optimal).
  5. Guidance: CFG, distilled guidance, image CFG, skip-layer guidance; why distilled and turbo
     models use CFG 1 and ignore negatives.
  6. Flow shift, custom sigmas, step caching (ucache/easycache/…) and their quality trade-offs.
  7. Hires pass vs Upscale vs a second refine pass; choosing upscalers.
  8. Masks, inpainting and compositing.
  9. LoRAs: strengths, step-reduction LoRAs, family compatibility.
  10. Seeds, sweeps, comparisons and reproducibility.
  11. Memory and time: model loads per run, VAE tiling, admission, the run preview.
  12. Calling a workflow through the API: listing, exposed inputs, `workflow_inputs`, step
      overrides, labelled outputs, version pinning, streaming.
  13. Limits and troubleshooting: validation messages and their fixes.
- Document `manage-workflows` through the ability definition (generated reference), and
  `/v1/workflows` plus the `workflow:` model name in the API reference guide.
- Update [Image generation](guide/web-app/image-generation.md) (choosing an advanced workflow,
  exposed inputs), [Image models](guide/models/image-models.md) (samplers/schedulers now
  validated against capabilities) and the navigation guide.
- Sample-specific notes live in the samples' Note nodes, not in the guide.
- Run `scripts/docs-coverage.py` and the documentation checks in
  [agents.md](agents.md#documentation-maintenance).

## Phases

| Phase | Deliverable | Acceptance |
|---|---|---|
| 0 | **Engine options.** Map sampler names to native names. Read samplers, schedulers, features and preview modes from `/sdcpp/v1/capabilities` per installed profile; offer every native sampler/scheduler in the existing workflow editor. Extend the request allowlist with `advanced` fields (eta, flow shift, shifted timestep, img CFG, SLG, custom sigmas, clip skip, VAE tiling, cache mode/option, hires) with bounds | Unit tests for name mapping and bounds; fake-engine test proves the native payload carries the chosen sampler; native qualification proves dpm++2m output differs from euler at a fixed seed |
| 1 | **Design proposals.** Three standalone previews (01 Node canvas, 02 Stage lanes, 03 Split studio) with sample graphs and the test-run panel, rendered by `scripts/preview-image-workflow-designs.py`; light/dark, 1440/390/320 px, empty/invalid/running/failed states | Owner selects a layout |
| 2 | **Graph documents and access.** Migration, `manage-workflows` ability, CRUD API with versions/publishing/deletion, labels and exposed/overridable fields, validator (types, cycles, bounds, capabilities), samples seeded as read-only templates | API, ability and validator tests; migration test on a copy of an existing DB |
| 3 | **Editor.** Selected layout live at Models → Image workflows: palette, canvas, inspector, Send to / Use from, automatic binding, Insert after, undo/redo, Outline, page-memory drafts; "Open as advanced" converts a copy of a simple preset | Browser tests at three widths, keyboard-only linking, UI consistency audit, all-page checks for shared parts |
| 4 | **Execution and API.** Compiler, per-model grouping with one-node preference, intermediate assets (24 h), per-stage progress, cancel/pause/resume, Compare and multiple Outputs; `model="workflow:<name>[@version]"` on the image endpoints, `workflow_inputs` and step overrides, `GET /v1/workflows`; test-run panel with API examples; Workflows group in Image generation | Fake-engine tests for ordering, fan-out, grouping, cross-node hand-off, resume without repeats, refusal of unexposed fields and out-of-bounds overrides, access following models, temporary runs; native qualification of samples 1–6 on the CUDA profiles, including an API call |
| 5 | **Structure and tiled upscale.** ControlNet (`controlnet`) and FLUX control images (`control_image`) with configured control models and preprocessed inputs; tiled detail upscaling with overlap and seam blending | Component validation for control models; samples 8a and 8b qualified, including seam inspection and memory peaks |
| 6 | **Video graphs.** Video sampler with high/low-noise section, Last frame, clip extension, video Compare; callable through `/v1/videos` with `workflow:` names | Samples 11–12 qualified; video limits respected |
| 7 | **Masks and detail.** Native `mask_image` inpainting per qualified family, Composite, ADetailer with converted YOLOv8 detectors | Protected pixels retained; detector component validation; samples 8 and 10 qualified |

Each phase updates its guide and the AGENTS.md template index in the same commit. Deployment, when
requested, uses `owg update; owg restart` and verifies the running source and live behavior.

## Risks

- **Pixel hand-off quality.** Repeated VAE round trips can soften detail. Mitigations: prefer the
  native hires pass, keep strength low, and measure in Phase 4 qualification.
- **Model swaps.** Graphs mixing models reload weights. The run preview shows loads, and the compiler
  groups stages per model.
- **Capability drift.** An engine upgrade can rename samplers or features. Validation always uses
  live capabilities; saved graphs with an unavailable value show a fix-it message, not a silent
  default.
- **Community defaults.** Values marked *community* stay suggestions until qualified on the
  owner's nodes.
- **API stability.** Callers depend on labels and exposed inputs. Publishing a version that removes or
  renames them is flagged in the editor, and pinned `@version` calls keep working.
- **Original design.** Interaction patterns are similar to node editors, but the UI, icons and
  names are this project's own. Nothing is copied from ComfyUI.

## Decisions (interview 2026-11-17)

| Topic | Decision |
|---|---|
| Location | Its own page: Models → Image workflows. Workflows are set up there and then called from the API |
| Simple presets | Keep the existing per-model presets; "Open as advanced" converts a copy into a one-sampler graph |
| ComfyUI import | Not now |
| Video | A later phase (Phase 6), after image graphs |
| API call | As a model name on the existing image endpoints: `model="workflow:<name>"` |
| Authoring | New ability `manage-workflows` |
| Running access | Follows the workflow's models only; no separate permission |
| Intermediates | Kept 24 hours unless an Output saves them |
| Multiple outputs | The API returns all Output images, labelled |
| API inputs | Standard image fields, named extra inputs and per-step overrides are all supported |
| Override addressing | By step label, e.g. `refine.steps`; only author-marked fields, within bounds |
| Placement | Normal routing per step; prefer one node when it has every model |
| Listing | Separate `GET /v1/workflows`; `/v1/models` stays models only. Image generation lists runnable workflows as a separate Workflows group |
| Priorities after the core | ControlNet/structure and tiled upscale (Phase 5) before masks/detail (Phase 7) |
| Design | Three standalone proposals first |
| Test runs | Built-in test-run panel with generated API examples |
| Delivery order | Phase 1 (proposals) first, then Phase 0, then Phase 2; commit and push after each; no deployment. Native GPU checks only after the GPUs have been idle for several minutes |
| Klein default | Keep 6 steps until a native comparison justifies a change |
| Version pinning | Yes: `workflow:<name>@<version>`; unpinned calls use the current version |
| Layout (2026-11-17) | **02 Stage lanes** is the live layout, because the component list grows long. Cards can be dragged (or moved with Alt + arrows) to a later lane or another place in a lane, never before a step they use; Tidy layout restores run order. Canvas and Outline remain as views |
| Upscalers (2026-11-17) | Each Upscale step names an installed upscaler file by file name, so a graph can chain or compare several. The hires pass may also name an installed upscaler file; Phase 4 must pass the image model's upscaler folder to `sd-server` (`--hires-upscalers-dir`) and resolve each file on the chosen node |

## Sources

- stable-diffusion.cpp at `95ff762`: [server API](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/examples/server/api.md),
  [caching](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/caching.md),
  [ADetailer](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/adetailer.md),
  [Flux 2](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/flux2.md),
  [Qwen Image](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/qwen_image.md),
  [Qwen Image 2.1](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/qwen_image_2.1.md),
  [Wan](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/wan.md),
  [LTX 2](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/ltx2.md).
- ComfyUI practice: [2-pass txt2img (hires fix)](https://comfyanonymous.github.io/ComfyUI_examples/2_pass_txt2img),
  [FLUX.2 klein in ComfyUI](https://blog.comfy.org/p/flux2-klein-4b-fast-local-image-editing),
  [FLUX.2 klein tutorial](https://docs.comfy.org/tutorials/flux/flux-2-klein),
  [Qwen Image Lightning 8-step workflow](https://www.runcomfy.com/comfyui-workflows/qwen-image-lightning-comfyui-workflow-8-step-fast-generation),
  [Wan 2.2 + Lightx2v](https://www.runcomfy.com/comfyui-workflows/wan-2-2-lightx2v-v2-comfyui-workflow-ultra-fast-text-to-video),
  [Wan 2.2 overview](https://comfyui.nomadoor.net/en/basic-workflows/wan-2-2/).
