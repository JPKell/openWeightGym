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
2. Complete the detected family's component checklist. Supply the required compatible encoders/VAE and optional upscaler, then select **Add model** below the fields. Existing installations use **Save model**.
3. Select the model under **Workflows**, edit **Workflow settings**, name it and choose **Save workflow**.

## Options

**Duplicate** creates a separate workflow. **Version history → Saved version → Use settings as draft** loads an earlier version into the editor; saving creates a new version. Workflow limits bound the choices offered in Image generation.

## MOSS-Image

Choose **MOSS-Image (Base / SFT)** for either variant. Supply its diffusion weights, **Qwen3.5 9B** text encoder, **Flux 2 VAE** and the external **Tokenizer JSON** from `OpenMOSS-Team/MOSS-Image-Base`, file `tokenizer/tokenizer.json`. The component checklist offers compatible local files and download links. The tokenizer is copied and verified with the rest of the model bundle; earlier Qwen tokenizers and text encoders do not fit.

Start with 1024 × 1024, 50 Euler steps, the discrete scheduler and CFG 3. Dimensions must be multiples of 16. These profiles generate from text and support negative prompts; set **Reference images** to 0. Prompt enhancement is not applied automatically. The existing native installer revision supports both variants; manual older engine builds may need updating through the existing image installer.

See the [native setup guide](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/moss_image.md) and [MOSS inference recommendations](https://github.com/OpenMOSS/MOSS-Image/blob/main/USERGUIDE.md).

## Limits and troubleshooting

Parts from different model families are not interchangeable. Read the checklist's reason when a component is missing or incompatible. An upscaler is needed when a workflow must enlarge a reference. The editor's drafts remain in page memory across switches; reloading loses unsaved edits. Saving configuration does not establish image quality or memory fit on a node.

## Related

[Image generation](/docs/web-app/image-generation) · [Files](/docs/model-guides/files)
