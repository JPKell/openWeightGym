<!--docs
title = "Image models"
summary = "Configure image components and versioned workflows."
order = 30
covers = ["/models/images"]
-->
# Image models

Configure image components and versioned workflows.

## Before you start

Use an account with [model management](/docs/system/abilities#ability-manage-models). Disabled platform services may hide media and download choices.

## Do it

1. Open **Models → Images** and choose **Add image model**. Select destination nodes and the diffusion file.
2. Complete the detected family's component checklist. Supply the required compatible encoders/VAE and optional upscaler, then select **Add model** below the fields. Existing installations use **Save model**. Type in the search field above a file list to narrow it; Enter chooses the first match. **Text encoder** lists every model file, including language models outside the image folders; the checklist says whether the chosen one fits. Each suggested **Use** button names its node. A copy on the diffusion file's node is offered first, so the bundle needs no transfer.
3. Select the model under **Workflows**, edit **Workflow settings**, name it and choose **Save workflow**.

## Options

Under **LoRAs**, **Add LoRA** adds a row whose **File** lists the library's LoRA adapters, labelled by node and folder like the component lists; choosing one fills an empty name from the file name. An existing model offers only copies on its own node. The checklist says whether each adapter fits the model's family.

**Duplicate** creates a separate workflow. **Version history → Saved version → Use settings as draft** loads an earlier version into the editor; saving creates a new version. Workflow limits bound the choices offered in Image generation. **Delete workflow** uses two-click confirmation and removes all its versions. You can delete your personal workflows; shared workflows require model-management permission. Saved jobs, resolved settings and outputs remain available. A concurrent edit requires reloading before deletion.

## MOSS-Image

Choose **MOSS-Image (Base / SFT)** for either variant. Supply its diffusion weights, **Qwen3.5 9B** text encoder, **Flux 2 VAE** and the external **Tokenizer JSON** from `OpenMOSS-Team/MOSS-Image-Base`, file `tokenizer/tokenizer.json`. The component checklist offers compatible local files and download links. The tokenizer is copied and verified with the rest of the model bundle; earlier Qwen tokenizers and text encoders do not fit.

Start with 1024 × 1024, 50 Euler steps, the discrete scheduler and CFG 3. Dimensions must be multiples of 16. These profiles generate from text and support negative prompts; set **Reference images** to 0. Prompt enhancement is not applied automatically. The existing native installer revision supports both variants; manual older engine builds may need updating through the existing image installer.

See the [native setup guide](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/moss_image.md) and [MOSS inference recommendations](https://github.com/OpenMOSS/MOSS-Image/blob/main/USERGUIDE.md).

## Krea 2

Choose **Krea 2 (Raw / Turbo)** for either variant; both share one architecture, so the checklist cannot tell them apart. Supply the diffusion weights, the **Qwen3-VL 4B** text encoder and the **Wan 2.1 VAE**. Comfy's `Comfy-Org/Krea-2` bundle ships that VAE as `vae/qwen_image_vae.safetensors`, and its `text_encoders/qwen3vl_4b_*` files fit. GGUF weights are in `realrebelai/KREA-2_GGUFs` (`BASE/` for Raw, `TURBO/` for Turbo) and `Qwen/Qwen3-VL-4B-Instruct-GGUF`. The text-only **Qwen3 4B** encoder used by Flux 2 klein does not fit.

Raw starts at 1024 × 1024, 20 Euler steps, the discrete scheduler and CFG 3.5. Turbo starts at 8 steps and CFG 1, where negative prompts have no effect. The setup dialog offers both suggestions; save a Turbo model's workflow with its own defaults. Turbo's native 2048 × 2048 output (4.2 megapixels) exceeds the default **Pixel budget** of 2,097,152; raise it in the model's limits to use that size. Dimensions must be multiples of 16. These profiles generate from text; set **Reference images** to 0. Reference editing needs a community editing LoRA and is not supported yet.

See the [native setup guide](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/krea2.md).

## Advanced workflows {#advanced-workflows}

Advanced workflows chain several steps (samplers, refine passes, upscaling and outputs) into one
versioned graph that callers run like a model. Creating, changing, publishing and removing them
needs the [manage-workflows](/docs/system/abilities#ability-manage-workflows) ability, which is
separate from model management.

- Each save is a new version. Saving fails when someone else saved first; reload and save again.
- One version is published at a time, and only a version without run problems (missing sources,
  uninstalled models, family limits, steps of later phases) can be published.
- Anyone allowed to use every model a published workflow uses may run it and sees only its
  inputs, overridable bounds and outputs. Drafts and history stay with workflow managers.
- Each Upscale step names an installed upscaler file (ESRGAN family, for example
  `4x-UltraSharp.pth`) and a factor; chain several to enlarge in stages, or feed one image to
  several into a Compare to choose between them. A Sampler's hires pass uses a latent method. A
  file no node has is a run problem.
- A Control image step needs a model entry with a [ControlNet](#controlnet); Tiled detail needs an SDXL model.
- The shipped samples are read-only templates; save a copy to change one.
- Removing a workflow keeps the images and job records it produced.

Build and publish them under **Models → Image workflows**; see
[Image workflows](/docs/model-guides/image-workflows). **Open as advanced** in a simple workflow's
actions converts a copy into a one-sampler graph. Test-run workflows there, or call a published
one through the API as `workflow:<name>`.

## ControlNet {#controlnet}

An SDXL model entry can load an optional **ControlNet** so image workflows can guide composition
with a control image. Choose the ControlNet file in the component checklist; it must be an SDXL
ControlNet (an SD 1.x one is refused), in the original or diffusers layout. The checklist offers
`diffusers/controlnet-canny-sdxl-1.0` for edge guidance. A ControlNet stays loaded beside the model
and adds its weights to the memory used, so only an exclusive entry (**Keep GPUs to itself**) may have one.
Make a separate exclusive entry for the same checkpoint when you also want a shared one. An entry
loads one ControlNet, so match the workflow's control kind to it: a canny ControlNet for edges, a
depth one for depth maps.

## Limits and troubleshooting

Parts from different model families are not interchangeable. Read the checklist's reason when a component is missing or incompatible. An upscaler is needed when a workflow must enlarge a reference. The editor's drafts remain in page memory across switches; reloading loses unsaved edits. Saving configuration does not establish image quality or memory fit on a node.

## Related

[Image generation](/docs/web-app/image-generation) · [Files](/docs/model-guides/files)
