<!--docs
title = "Image generation"
summary = "Generate pictures, use references and review your private image queue."
order = 35
covers = ["/chat/images"]
-->
# Image generation

Create or edit images using a permitted image model and workflow.

## Do it

1. Open **Image generation**, choose a model and workflow, and write **Prompt**.
2. Set the image size and offered sampling controls. Add **Reference images** only when the workflow supports them.
3. Select **Generate**. Live previews appear on the main canvas even when you have saved images. Use **View images** to browse your results and **View preview** to return to the current generation. Turning off **Live previews** shows saved images instead.

## Review images and runs

**Gallery** puts the selected image in the main viewer with a narrow, scrollable thumbnail list
on the right. Click a thumbnail or use the arrow keys to move between images; navigation follows
the current image filter. Download, reuse, favorite and delete controls sit below the image.
**Library** keeps the larger thumbnail grid and batch actions. **Queue** shows one compact row
per job with status, progress and pause/stop controls below the main viewer.

Press **Delete** to arm **Delete image**, then press **Delete** again to confirm. Holding the
key does not repeat deletion. Escape cancels; typing in a form or viewing a live preview never
deletes a saved image. Image details and settings export remain below the viewer.

## Options

| Control | Use |
|---|---|
| **Sampling steps**, **Prompt guidance**, **CFG scale** | Controls vary by model family; use only those offered. |
| **Seed** | Reuse a seed when comparing a parameter change. |
| **Composite mask · PNG** | White or transparent areas take the generated result; other areas preserve the first reference. The mask blends after generation. |
| **Set up a parameter sweep** | Open **Explore variations** and choose **Vary** and **Values · comma separated**. Advanced users can supply **Multiple axes · JSON**. |
| **Temporary · no retained history** | Available only when your account and current compute policy permit no-log generation; temporary references need a temporary job. |

**MOSS-Image Base / SFT** offers CFG, negative prompts and text layout controls. For its required components and starting settings, see [MOSS-Image setup](/docs/model-guides/image-models#moss-image).

## Library downloads and deletion

Tick the checkboxes on image thumbnails to select several images, or use **Select all shown**.
**Download selected** saves one ZIP containing the original images (up to 200 images and 256 MiB
per archive). Selection survives library refreshes; changing the library filter clears it.

**Delete image**, saved-reference **Delete**, and **Delete selected** require two clicks on the
same button. The first click asks for confirmation; the second deletes. Escape, a changed selection,
or ten seconds without confirmation cancels it. Deletion remains limited to your own images and
refuses images used by unfinished jobs. If some selected images cannot be deleted, they stay selected
and the error reports how many succeeded.

## Resize and add detail {#resize}

Choose **Resize & add detail**, select an uploaded or saved source and choose the final size. This needs an exclusive SDXL source-image workflow. Enlargement requires the configured AI upscaler; its failure stops the job. The original source remains available. Review changed fine details before using the new image.

## Limits and troubleshooting

Reference count, preparation size and output choices depend on the selected workflow. A reference marked **Too small** needs a suitable upscaler or a larger source. A **Not used** reference is outside that workflow's supported inputs. **Generate** stays disabled while inputs are incompatible.

Regular results are kept in your private library. Queue controls act on remaining work; completed images remain available. Administrators configure model components and workflows in **Models → Images**.

## Related

[Image extraction](/docs/web-app/image-extraction) · [Voice and media](/docs/web-app/voice)
