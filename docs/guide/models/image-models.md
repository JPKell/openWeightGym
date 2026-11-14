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
2. Complete the detected family's component checklist. Supply the required compatible encoders/VAE and optional upscaler, then select **Save model**.
3. Select the model under **Workflows**, edit **Workflow settings**, name it and choose **Save workflow**.

## Options

**Duplicate** creates a separate workflow. **Version history → Saved version → Use settings as draft** loads an earlier version into the editor; saving creates a new version. Workflow limits bound the choices offered in Image generation.

## Limits and troubleshooting

Parts from different model families are not interchangeable. Read the checklist's reason when a component is missing or incompatible. An upscaler is needed when a workflow must enlarge a reference. The editor's drafts remain in page memory across switches; reloading loses unsaved edits. Saving configuration does not establish image quality or memory fit on a node.

## Related

[Image generation](/docs/web-app/image-generation) · [Files](/docs/model-guides/files)
