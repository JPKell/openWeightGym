<!--docs
title = "Mirror Wall"
summary = "Image generation with stable-diffusion.cpp: how an image is made, the models and their parts, and adding one."
order = 80
covers = []
aliases = ["/docs/mirrorwall"]
code = "mirrorwall"
-->
# Mirror Wall

## Overview {#overview}

Mirror Wall makes images on the compute machine with stable-diffusion.cpp. You write a prompt under [Chat → Images](/chat/images), optionally add reference images, and the finished pictures are kept privately on the control node. Administrators set up models under [Models → Images](/models/images).

## How an image is made {#workflow}

Every request takes the same path. Control node checks it and queues it; LoadCoach reserves the cards and memory the whole job needs before anything loads; sd.cpp then runs the stages in order, loading each part only when its stage starts, so the text encoder and the diffusion weights need not be on the card at the same time.

Swipe across the diagram to see all of it.

- **Check and queue.** Control node refuses at once what would fail later: more reference images than the model uses, a reference too small for a model without an upscaler, or a mask without a reference.

- **Admission.** LoadCoach reserves the whole job's measured peak on each card plus headroom. A model without a measurement yet takes its cards to itself.

- **Prepare references.** Each reference is scaled to the model's reference size, keeping its shape. A smaller one is first enlarged with the upscaler; an ordinary resize is never used to enlarge.

- **Encode the prompt** with the text encoder (SDXL: its built-in CLIP L and CLIP G).

- **Sample.** The diffusion weights turn noise into a latent image over the chosen steps, guided by the prompt, any LoRAs and the references. Live previews show the picture forming.

- **Decode** the latent image to pixels with the VAE.

- **Mask.** With an edit mask, white or transparent areas take the new picture and the rest keeps the first reference. This is a composite after generation, not inpainting.

- **Upscale** the output 2× or 4× with the upscaler, if asked.

- **Keep.** The PNG goes into your private library on the control node; nothing about the prompt is written into the file.

## Models and their parts {#models}

An image model is several files that must belong together. The diffusion weights decide everything else: each size of Flux 2 reads prompts with its own text encoder, and all of them decode with the Flux 2 VAE. Sizes are on disk; VRAM is the measured peak of the whole job on this machine's 16 GB cards.

| Model | Good at | Parts it needs | On this machine | References | Starting settings |
| --- | --- | --- | --- | --- | --- |
| Flux 2 klein 9B qualified | The everyday model: quick, good-quality pictures and edits from reference images. | Diffusion weights: `flux-2-klein-9b` GGUF, Q4_0 5.3 GB Text encoder: **Qwen3 8B**, 4.3–5.0 GB VAE: **Flux 2 VAE**, 0.3 GB Upscaler: optional | One card, 9.0 GiB peak, 1.4 GiB host RAM (`flux2-klein-q4`); or split 8.1 + 5.7 GiB across two cards (`flux2-klein-split-q4`) | Up to 2, 1 MP | 6 steps, guidance 1, euler / flux2. The *base* variant: about 20 steps and CFG 4. |
| Flux 2 klein 4B accepted, not yet run here | The quickest drafts, and the one to choose on a smaller card. | Diffusion weights: `flux-2-klein-4b` GGUF, Q4_0 2.5 GB or Q8_0 4.3 GB Text encoder: **Qwen3 4B**, 4.3 GB at Q8_0 VAE: **Flux 2 VAE** Upscaler: optional | Expect well under one card | Up to 2 | 4–6 steps, guidance 1. Base variant: about 20 steps and CFG 4. |
| Flux 2 Dev qualified | The most detail and the closest prompt following: busy scenes, text in images, edits that combine two references. | Diffusion weights: `flux2-dev` GGUF, Q4_K_S 19.3 GB Text encoder: **Mistral Small 3.2 24B**, Q4_K_M 14.3 GB VAE: **Flux 2 VAE** Upscaler: optional | Both cards, layer split: 13.1 + 13.3 GiB peak, 2.3 GiB host RAM, 12 GiB host budget (`flux2-dev-q4`) | Up to 2, at 0.5 MP (two 1 MP references do not fit here) | 28 steps, guidance 4 |
| SDXL checkpoint accepted, not yet run here | The thousands of SDXL fine-tunes and styles on Civitai; negative prompts. | One file with the UNet, CLIP L, CLIP G and VAE inside, about 6.9 GB VAE: optional `sdxl-vae-fp16-fix`, 0.3 GB, which avoids black or noisy images Upscaler: optional | Expect one card | 1 start image, with strength (image-to-image) | 28 steps, CFG 7; dpmpp2m with karras is a common choice |
| SDXL UNet only | As above, when a release ships its parts separately. | UNet weights VAE: SDXL VAE (4 channels) **CLIP L** and **CLIP G** text encoders | Expect one card | 1 start image | As above |

**Qwen Image (original / 2512)** supports text-to-image generation with a Qwen2.5-VL 7B encoder and the 16-channel Qwen Image VAE. Suggested start: 1024 × 1024, 50 steps, CFG 4, euler / discrete. **Qwen Image 2.1** uses Qwen3-VL 8B and its own 64-channel VAE: start with CFG 6, euler / flux and dimensions divisible by 32. These suggestions need measurement on the selected node; no memory fit or image quality qualification is implied.

Qwen profiles currently generate from text only. Qwen Image Edit, Layered and Turbo recipes need additional conditioning or schedules and are not enabled by these profiles. The Qwen3 text-only encoder used by Flux is not a substitute for a Qwen vision-language encoder. [Qwen Image component reference](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/qwen_image.md) · [Qwen Image 2.1 component reference](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/qwen_image_2.1.md).

Not run by Mirror Wall: Flux 1, Stable Diffusion 1.x, 2.x and 3, and the other families stable-diffusion.cpp knows. The checklist on Models → Images says so when it reads one.

### What each part does

**Diffusion weights**

The model itself: it draws the picture, step by step. Its size decides which text encoder it needs.

 **Text encoder**

Turns the prompt into what the diffusion weights read. It must be the one the model was trained with: a Flux 2 klein 9B with the Qwen3 4B encoder does not work. Any quantization of the right model will do.

 **VAE**

Turns the finished latent image into pixels, and reference images into latents. Flux 2 uses a 32-channel VAE; SDXL a 4-channel one. They are not interchangeable.

 **CLIP L, CLIP G**

SDXL's two text encoders. A full SDXL checkpoint has them inside; leave these empty for it and for every Flux 2 model.

 **Upscaler**

An image enlarger such as 4x-UltraSharp (67 MB). Without one, reference images must already be at least the reference size, and outputs cannot be upscaled.

 **LoRA**

A small add-on that teaches a style or subject. It works only on the base it was trained for: a klein 9B LoRA on klein 9B.

### Where to get them

- Flux 2 klein: `leejet/FLUX.2-klein-9B-GGUF`, `leejet/FLUX.2-klein-4B-GGUF`; text encoders `unsloth/Qwen3-8B-GGUF`, `unsloth/Qwen3-4B-GGUF`.

- Flux 2 Dev: `city96/FLUX.2-dev-gguf`; text encoder `unsloth/Mistral-Small-3.2-24B-Instruct-2506-GGUF`.

- Flux 2 VAE: `split_files/vae/flux2-vae.safetensors` in `Comfy-Org/Flux-2-Dev`.

- SDXL: `stabilityai/stable-diffusion-xl-base-1.0`, or a fine-tune from [Civitai](/civitai); VAE `madebyollin/sdxl-vae-fp16-fix`.

- Upscaler: `4x-UltraSharp.pth` in `Kim2091/UltraSharp`.

## Adding a model {#adding}

[Models → Images](/models/images) combines the model navigator, its workflows and an editor. *Add image model* or *Edit model setup* opens the files, hardware, defaults and limits on the same page. When adding, select the destination compute nodes in the form header. The installer checks every selected node before starting, copies and verifies the complete bundle, and creates a model on each destination after its files are ready. Installation progress is shown for every selected node. Hardware settings must be valid on all destinations; edits remain scoped to the node being browsed. Choose *New workflow* to create another named set of image settings without duplicating the model files or GPU allocation.

Workflow edits create a new saved version. *Duplicate* creates a separate workflow; *Version history* loads earlier settings into a draft, which can be saved as a new version. Revisions overwritten before history was introduced cannot be reconstructed. Drafts stay in page memory across model switches; reloading the page drops unsaved changes. Shared workflow visibility is fixed after creation; duplicate to change it. Non-managers can save personal copies of shared workflows.

- **Bring the diffusion weights down.** Find them on the [Hugging Face](/hub) page (type the repository, tick the file) or on [Civitai](/civitai). Downloads arrive in the control node's library.

- **Copy it to the compute node.** On [Files](/files), copy the file to compute, into the image folder. Files on the node are what a model can use.

- **Start the model.** Press *Configure* beside the file on Files, or [Models → Images](/models/images) → *Add image model* and paste its path into Diffusion weights.

- **Read the checklist.** It reads each file's header on the node (never the weights) and shows what the model is, such as “Detected: Flux 2 klein 9B”, and sets the family. Each part is marked:

- ✓ right for this model;

- ✗ missing or wrong, with what it needs instead, for example “This is a Qwen3 4B text encoder; it needs the Qwen3 8B text encoder”;

- ○ optional, with what it adds;

- ! given but not used by this model: leave it empty.

 Under each missing part, *Use …* buttons offer matching files already on the node, and *Get …* opens the Hugging Face page at the repository that has it. Bring those down and copy them across the same way.

- **Choose the cards.** One GPU suits klein; Flux 2 Dev needs both cards with *diffusion layers* split. Leave the host RAM cap at 8 GiB unless the job needs more (Dev uses 12 GiB).

- **Save, then try it** in [Chat → Images](/chat/images). The first full job is measured; until then the model keeps its cards to itself, so other models wait.

## Canvas studio {#canvas}

Choose a model and workflow on the left, describe the image beneath the canvas, and adjust size, sampling and references in the settings inspector. Unsent changes stay with each model while you switch models or views. Generate, Library and Queue keep the same workspace; new results do not replace your selected image. Use Compare to view another owned image alongside it.

Explore variations offers a simple parameter sweep and optional JSON for multiple axes. Queue supports pause after an image, resume and stop; temporary jobs support stop only. Image details exposes the resolved seed and settings, with explicit JSON export. Session favorites last only while this page is open.

## Resize and add detail {#resize}

Choose *Resize & add detail*, upload one image or select a saved source, and choose 2×, 4×, same-size refinement or custom final dimensions. Keep source proportions enabled to preserve its shape. Detail strength starts at 0.25; lower values preserve more of the source. Optional instructions describe the details to refine. This generates new detail and may change fine features.

The tool requires an exclusive SDXL workflow with one source-image slot. It runs image-to-image at the workflow’s permitted working size, uses the configured AI upscaler whenever enlargement is needed, and returns the exact final dimensions. A missing or failed upscaler stops the job. Final output is limited to 8192 pixels per side, 32 megapixels and the workflow’s upscale limit, at most 4× the detail-pass dimensions. The original remains unchanged and private; temporary sources require a temporary job.

## Reference images {#references}

The Images page shows, for the chosen model, whether it takes references, how many, and marks each image: 1 · main used, Enlarged small but the model can enlarge it, Too small or Not used. Generate stays off, with the reason beneath it, until the references fit.

| Model | How it uses them | How many | Tips |
| --- | --- | --- | --- |
| Flux 2 klein, Flux 2 Dev | Reference conditioning: the model looks at them while it draws a new picture. | Up to 2 (the model's limit) | Order matters; image 1 is the main one. Say what to take from each: “the person from image 1 wearing the jacket from image 2”. |
| SDXL | Image-to-image: it starts from the one image and redraws it. | 1 | Strength 0.3–0.5 keeps the picture; 0.7–0.9 follows the prompt. |

- **Size.** Each reference is scaled to the reference size (1 MP for klein, 0.5 MP for Dev here), keeping its shape. Smaller images need the model's upscaler; without one, use larger images or lower the reference size under Advanced settings.

- **Shape.** Between 1:8 and 8:1; crop anything longer before uploading. Transparent areas become white.

- **Edit mask.** A PNG the size of the first reference. White or transparent areas are replaced by the new picture; the output must keep the first reference's shape.

- **Privacy.** References are yours alone. Prepared copies are kept in memory for an hour so that a sweep does not enlarge the same image for each seed; with *Temporary* nothing is kept.

## When something goes wrong {#trouble}

**“Reference 1 is smaller than this model's reference size and the model has no upscaler”**

Use a larger image, lower Reference size under Advanced settings, or add an upscaler to the model on Models → Images.

 **“Flux 2 requires explicit VAE and text-encoder components”**

Fill both on Models → Images; the checklist offers matching files.

 **Black, grey or noisy SDXL images**

Set the VAE to `sdxl-vae-fp16-fix`.

 **The job waits a long time before starting**

An unmeasured model takes its cards to itself, so it waits for work on them to finish. After its first full job it can share.

 **“No available image model”**

The sd.cpp runtime is not installed on the node, or no image model may be used by your group. Administrators: `scripts/install-sdcpp.sh`, then Models → Images.

 **A LoRA changes nothing**

It is for another base model; the checklist marks it with ✗. LoRA strength 0 also turns it off.
