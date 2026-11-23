<!--docs
title = "Image workflows"
summary = "Build multi-step image workflows in stage lanes, test-run them, then publish them for workflow: API calls."
order = 30
covers = ["/models/image-workflows"]
-->
# Image workflows

Build multi-step image workflows in stage lanes, test-run them, then publish them for
`workflow:<name>` API calls.

## Before you start

Use an account with the [manage-workflows](/docs/system/abilities#ability-manage-workflows) ability.
Models are installed separately under **Models → Images** with
[model management](/docs/system/abilities#ability-manage-models).

## Do it

1. Open **Models → Image workflows**, under the **Image setup** heading. Choose a sample from the chooser, or **New workflow**,
   which starts with a prompt, model, seed, size, sampler and output, already connected.
   To start from a simple per-model workflow, open it under **Models → Images** and choose
   **Open as advanced**; the simple workflow is unchanged. Its model, size, sampling settings,
   negative prompt and a fixed seed carry over, and an enlargement becomes an Upscale step whose
   upscaler you choose. Notes on the canvas name what does not carry over, such as LoRA strengths
   and the number of images, which a caller sets with `n`.
2. Add steps from **Add a component**. A new step connects to the obvious sources; with a step
   selected, **Add after** inserts one between it and its destinations.
3. Select a step to edit it on the right. **Use from** chooses each input's source and
   **Send to** ticks every destination of an output. Links that would form a loop are refused.
4. Mark fields callers may set (**Callers set this**) or override within bounds
   (**Callers may override**). Callers address overrides by step label, such as `refine.steps`.
   Changing a step's **Step label** keeps its links: every source and destination follows the new label.
5. Fix everything listed under the lanes, then try it with **Run test** (see below). The list
   names anything a run would refuse: a value outside its field's range, a step planned for a
   later phase, a step cache the model's family does not take, and advanced settings on a
   shared-residency model. **Run test** stays off until they are fixed.
6. **Save version** and **Publish**.

## Test runs

**Run test** runs the workflow on screen with the values in the run panel: the saved version
when nothing changed, otherwise the unsaved changes as they are. Each step shows its progress
and the node that ran it; Output and Compare images are saved to your image library and shown
below the steps. **Stop** abandons the step in progress and starts nothing after it; finished
images stay. Load image steps choose from your reference images. A text field cleared in the
run panel is sent empty, so a cleared separator joins texts with nothing; an empty number keeps
the saved value. At most two test runs of yours
run at a time, and a finished run's progress is kept in memory for an hour.

## Lanes, canvas and outline

Each lane runs after the lanes to its left. Drag a card by its title to another lane or another
place in its lane, or focus it and press Alt with an arrow key. A card never moves before a step it
uses; moving a source later moves the steps that use it along. **Tidy layout** returns every card
to run order. **Canvas** places cards freely, and **Outline** lists the workflow in run order for
keyboards, screen readers and narrow screens, which start there. **Undo** and **Redo** (Ctrl+Z,
Ctrl+Shift+Z) cover every change, including moves.

## Steps and images

Each Sampler or Upscale step is one ordinary image request, admitted, routed, logged and counted
like any other, so a run's steps appear in the request log one by one. Resize and Combine run on
the control node. Steps on one model run together, so it stays loaded. An Upscale step runs on
the node that made its image when that node holds the upscaler file. Intermediate images stay in
the run's memory; only Outputs are saved, with the workflow version and every step's settings. Images, not latents, pass between
steps, so a second sampler fed by the first is an image-to-image pass whose **strength** sets how
much it changes; at 0.5 or lower the extra decode and encode is rarely visible. A Sampler's
**Hires pass** keeps enlargement inside one request instead. Only SDXL models accept a start image
here; Flux 2 models take up to four ordered reference images.

## Samplers, schedulers and guidance

Every sampler and scheduler of the installed engine is offered by its native name. Ancestral and
SDE samplers (`_a`, `sde`, `er_sde`) add noise each step, so results vary more between step
counts. Flow-matching families usually suit `flux`, `flux2`, `simple` or `beta` schedulers; SDXL
suits `karras`, `exponential` or `kl_optimal`. Distilled and turbo models run at CFG 1, where the
negative prompt has no effect. **Advanced settings** hold eta, flow shift (not SDXL), image CFG,
shifted timestep and CLIP skip (SDXL only), step caching and VAE tiling.

## Upscalers

An **Upscale** step names an installed upscaler file (ESRGAN family, such as
`4x-UltraSharp.pth`) and a factor; the list shows which nodes hold each file. Chain several
Upscale steps to enlarge in stages, or send one image to several and into a **Compare** to choose
between them, as the *Upscaler comparison* sample does. A hires pass uses a latent method; a hires pass
that names an upscaler file is listed as a problem until hires upscaler files can run. A file no
node has is also a problem to fix before publishing.

## Structure from a control image

A **Control image** step guides a Sampler's composition with the ControlNet its model loads. Feed
it an image and link its output to the Sampler's **Control image** input. **Edges** finds the edges
in any photo or drawing; the runtime detects them. **Depth** and **Pose** use the image as given,
so supply a ready-made depth map or pose skeleton: this app does not estimate depth or pose. Match
the kind to the ControlNet, for example **Edges** with a canny ControlNet. **Control strength**
(0.6–0.8 is a good start) sets how closely the result follows the structure. The control image is
fitted to the Sampler's size by cropping its centre.

Only an exclusive SDXL model entry with a **ControlNet** component can take a control image; add
one under **Models → Images** (see [Image models](/docs/model-guides/image-models#controlnet)).
Other models show the problem *This model has no ControlNet*. The *Structure-guided generation*
sample uses edges at strength 0.7.

## Tiled detail

A **Tiled detail** step adds detail to a large image, typically one an Upscale step enlarged. It
splits the image into tiles of at most **Tile size** pixels a side, sharing at least **Overlap**
pixels with their neighbours. Each tile then goes through a low-strength image-to-image pass (0.2–0.35 keeps
the composition), and the tiles are blended back with a gradual fade across each overlap so no
seams show. Each tile is one image request on an SDXL model with the step's steps, sampler,
scheduler and CFG; a linked Seed gives every tile the same seed. One step makes at most 64 tiles
per image; raise the tile size for larger images. The *Tiled 4× upscale* sample enlarges 2×, adds
detail in nine 1024-pixel tiles, then enlarges 2× again.

## Versions and publishing

- Each save is a new version. Saving fails when someone else saved first; reload and save again.
- **Versions** opens an earlier version as a draft; saving it makes a new version and keeps the
  history.
- One version is published at a time, and only a version without problems can be published.
  Callers keep running the published version until another is published.
- Anyone allowed to use every model a published workflow uses may run it. Drafts and history stay
  with workflow managers.
- Samples are read-only; saving one creates your own copy. Delete asks for a second click and
  keeps the images and job records a workflow produced.

## Calling a workflow from the API

Send a published workflow's name as the model to `/v1/images/generations` (or `/v1/images/edits`
with images). The run panel's **Call this workflow from the API** shows the exact call.

```json
{"model": "workflow:sdxl-two-pass-hires", "prompt": "A lighthouse at dusk", "seed": 7, "n": 2,
 "workflow_inputs": {"base.steps": 30, "base.hires_denoise": 0.3}}
```

- `prompt`, `negative_prompt`, `seed`, `size` and `image` (or `image[]`) set the first exposed
  input of their kind. `workflow_inputs` sets any other exposed input by step label and field
  (`style.text`), a Load image step by its label, and overridable fields within their bounds.
  Anything the workflow does not expose is refused with the reason, never ignored.
- `n` (1–16) runs the whole workflow that many times; a random seed moves on by one per run,
  as does an *increment* seed.
- The response's `data` lists every Output and Compare image with its `output` label, `step`,
  `item`, `seed`, size, `asset_id` and the settings of every step (`stages`). With
  `"stream": true`, `workflow.step` and `workflow.progress` events report each step, then each
  image arrives as `image_generation.completed`, and `workflow.completed` ends the stream.
- `workflow:<name>@<version>` runs a once-published version; without it, the current published
  version runs.
- `GET /v1/workflows` lists the workflows you may run, with their inputs, overridable fields and
  bounds, outputs and models.
- `x-loadcoach-log: off` runs without logs where your account allows no-log generation.

## Limits and troubleshooting

A workflow holds at most 64 components, 128 links and 32 steps, and one call runs at most 128
steps in all (steps × `n`). Unsaved changes stay in page
memory when you switch workflows, but reloading the page loses them. Steps marked with a later
phase (masks, composite, face detail) can be saved but not run or published. The
editor does not set LoRA strengths yet; a converted simple workflow notes them on the canvas.
A step that fails stops the run and is named in the error, for example
`Refine: That upscaler is not installed on this node.` A shared-residency model refuses a step
that uses advanced settings (the editor lists it before a run), or a CFG other than 1 when its profile was measured
at CFG 1; use an exclusive model entry for those steps. A Tiled detail output records the seed its tiles used. Image generation does not list workflows yet; run them here or through the
API.

## Related

[Image models](/docs/model-guides/image-models) · [Image generation](/docs/web-app/image-generation)
