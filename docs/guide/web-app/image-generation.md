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

**Gallery** gives the selected image the full available frame height, without a heading, with a
narrow, scrollable thumbnail list on the right. Click a thumbnail or use the arrow keys to move
between images; navigation follows the current image filter. Download, reuse, favorite, delete,
comparison and viewing controls sit below the image, followed by links to the other workspace views.
**Library** keeps the larger thumbnail grid and batch actions. **Queue** shows one compact row
per job with status, progress and pause/stop controls below the main viewer.

The queue finishes all images and sweep variations in a job before starting the next job for
that model. Jobs run in parallel only on different models that are already loaded; a job that
needs another model loaded waits for the current job to finish. Pausing releases the model's
turn after its current image, and cancelling releases it immediately. Temporary jobs follow
the same ordering without retaining job history.

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

**Krea 2 Raw / Turbo** offers CFG and negative prompts; Turbo runs at CFG 1, where negative prompts have no effect. See [Krea 2 setup](/docs/model-guides/image-models#krea-2).

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

## Samplers and advanced sampling {#sampling}

**Sampler** and **Scheduler** list every method of the installed native engine: 21 samplers
(Euler, DPM++ 2M, DPM++ 2M SDE, ER-SDE, LCM, TCD, res_multistep and others) and 16 schedulers
(discrete, Karras, exponential, simple, SGM uniform, beta, Flux and others). Saved workflows
that still use the earlier names `dpmpp2s_a`, `dpmpp2m` and `dpmpp2mv2` run as `dpm++2s_a`,
`dpm++2m` and `dpm++2mv2`. An engine that does not report a requested method refuses the job;
it no longer falls back to its default.

API callers of an exclusive model entry can add an `advanced` object to
`/v1/images/generations`. Shared-residency entries keep their measured profile and refuse it.

| Field | Range | Applies to |
|---|---|---|
| `eta` | 0–2 | Ancestral and SDE samplers |
| `flow_shift` | 0–100 | Flow-matching families (Flux, Qwen, Krea, MOSS); not SDXL |
| `shifted_timestep`, `clip_skip` | 0–1000; 1–12 (integers) | SDXL only |
| `img_cfg` | 0–30 | Image guidance for reference editing |
| `slg` | `scale` 0–10, `layer_start` < `layer_end` (0–1), optional `layers` (1–16 numbers) | DiT families; not SDXL |
| `custom_sigmas` | `steps` + 1 values, from high to low | Replaces the scheduler's noise levels |
| `vae_tiling` | true/false | Lower decode memory for large images |
| `cache_mode`, `cache_option` | `ucache`/`spectrum` for SDXL; `easycache`, `dbcache`, `taylorseer`, `cache-dit`, `spectrum` for the others; options such as `threshold=0.2` | Faster steps with some loss of detail |
| `hires` | `upscaler` (Latent variants, Lanczos, Nearest), `scale` 1–4, `steps`, `denoising_strength` 0–1 | A second pass at the larger size within the model's limits; not combined with Upscale or Resize & add detail |

```json
{"model": "sdxl-photo", "prompt": "A lighthouse at dusk", "steps": 28, "sampler": "dpm++2m", "scheduler": "karras",
 "advanced": {"hires": {"scale": 1.5, "steps": 14, "denoising_strength": 0.38}, "vae_tiling": true}}
```

## Limits and troubleshooting

Reference count, preparation size and output choices depend on the selected workflow. A reference marked **Too small** needs a suitable upscaler or a larger source. A **Not used** reference is outside that workflow's supported inputs. **Generate** stays disabled while inputs are incompatible.

Regular results are kept in your private library. Queue controls act on remaining work; completed images remain available. Administrators configure model components and workflows in **Models → Images**.

## Related

[Image extraction](/docs/web-app/image-extraction) · [Voice and media](/docs/web-app/voice)
