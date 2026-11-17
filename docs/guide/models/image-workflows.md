<!--docs
title = "Image workflows"
summary = "Build multi-step image workflows in stage lanes, then save, version and publish them."
order = 30
covers = ["/models/image-workflows"]
-->
# Image workflows

Build multi-step image workflows in stage lanes, then save, version and publish them.

## Before you start

Use an account with the [manage-workflows](/docs/system/abilities#ability-manage-workflows) ability.
Models are installed separately under **Models → Images** with
[model management](/docs/system/abilities#ability-manage-models).

Workflows can be designed, checked, saved and published now. Running them (test runs and
`workflow:<name>` API calls) arrives with workflow execution; the run panel already shows the
run plan and the exact API call callers will send.

## Do it

1. Open **Models → Image workflows**. Choose a sample from the chooser, or **New workflow**,
   which starts with a prompt, model, seed, size, sampler and output, already connected.
   To start from a simple per-model workflow, open it under **Models → Images** and choose
   **Open as advanced**; the simple workflow is unchanged.
2. Add steps from **Add a component**. A new step connects to the obvious sources; with a step
   selected, **Add after** inserts one between it and its destinations.
3. Select a step to edit it on the right. **Use from** chooses each input's source and
   **Send to** ticks every destination of an output. Links that would form a loop are refused.
4. Mark fields callers may set (**Callers set this**) or override within bounds
   (**Callers may override**). Callers address overrides by step label, such as `refine.steps`.
5. Fix everything listed under the lanes, then **Save version** and **Publish**.

## Lanes, canvas and outline

Each lane runs after the lanes to its left. Drag a card by its title to another lane or another
place in its lane, or focus it and press Alt with an arrow key. A card never moves before a step it
uses; moving a source later moves the steps that use it along. **Tidy layout** returns every card
to run order. **Canvas** places cards freely, and **Outline** lists the workflow in run order for
keyboards, screen readers and narrow screens, which start there. **Undo** and **Redo** (Ctrl+Z,
Ctrl+Shift+Z) cover every change, including moves.

## Steps and images

Each Sampler, Upscale or detail step is one native request. Images, not latents, pass between
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
between them, as the *Upscaler comparison* sample does. A hires pass can use a latent method or
an installed upscaler file. A file no node has is a problem to fix before publishing.

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

## Limits and troubleshooting

A workflow holds at most 64 components, 128 links and 32 steps. Unsaved changes stay in page
memory when you switch workflows, but reloading the page loses them. Steps marked with a later
phase (control images, tiled detail, masks, face detail) can be saved but not published. The
editor does not set LoRA strengths yet; a converted simple workflow notes them on the canvas.

## Related

[Image models](/docs/model-guides/image-models) · [Image generation](/docs/web-app/image-generation)
