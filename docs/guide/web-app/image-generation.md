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
3. Select **Generate**. Inspect the result on the canvas, use **Compare** for another owned image, and review **Library** or **Queue** without losing your selected picture.

## Options

| Control | Use |
|---|---|
| **Sampling steps**, **Prompt guidance**, **CFG scale** | Controls vary by model family; use only those offered. |
| **Seed** | Reuse a seed when comparing a parameter change. |
| **Composite mask · PNG** | White or transparent areas take the generated result; other areas preserve the first reference. The mask blends after generation. |
| **Set up a parameter sweep** | Open **Explore variations** and choose **Vary** and **Values · comma separated**. Advanced users can supply **Multiple axes · JSON**. |
| **Temporary · no retained history** | Available only with the installation's no-log permission; temporary references need a temporary job. |

## Resize and add detail {#resize}

Choose **Resize & add detail**, select an uploaded or saved source and choose the final size. This needs an exclusive SDXL source-image workflow. Enlargement requires the configured AI upscaler; its failure stops the job. The original source remains available. Review changed fine details before using the new image.

## Limits and troubleshooting

Reference count, preparation size and output choices depend on the selected workflow. A reference marked **Too small** needs a suitable upscaler or a larger source. A **Not used** reference is outside that workflow's supported inputs. **Generate** stays disabled while inputs are incompatible.

Regular results are kept in your private library. Queue controls act on remaining work; completed images remain available. Administrators configure model components and workflows in **Models → Images**.

## Related

[Image extraction](/docs/web-app/image-extraction) · [Voice and media](/docs/web-app/voice)
