# Video generation plan

Status: implemented and locally validated, 2026-11-15. V1–V9 approved; 01 Canvas selected for
both pages. The owner accepted the WAN smoke clip. Deployment remains excluded until requested.

## Delivery record — 2026-11-15

| Milestone | Result |
|---|---|
| M0 | All recommended decisions approved; both Canvas layouts selected. |
| M1–M3 | Browser containers rebuilt through the existing installer; recipe-based video runtime, exclusive batch admission, API, storage and usage implemented. |
| M4–M6 | Six offline proposals and both selected live templates implemented; settings and group overrides use the shared catalogue. |
| M7 | WAN 2.1 T2V 1.3B smoke-qualified at 832×480, 33 frames, 16 fps, 20 steps on one GPU. Cancellation/crash cleanup and Chrome/Firefox playback passed; owner accepted the initial smoke clip. Other seven qualification targets are not runnable because required weights are absent; their GPU fit, soundtracks and visual quality remain unqualified. Missing components and runtime evidence are explained in the model editor/studio. Larger WAN bounds and multi-card splitting are not qualified. |
| M8 | Guides, generated-route/setting sources and template index updated; 86/86 pages covered, zero documentation gaps. |

Evidence is retained under `data/qualification/video-wan21-qualified-20261115/` (current-build
smoke, browser playback and ordered profile inventory), `video-cleanup-20261115/` (same-build
cancellation/crash checks), `video-wan21-20261114/` (owner-accepted clip) and
`video-audio-container-20261115/` (synthetic PCM→Opus browser check, not a model soundtrack
qualification). The current smoke observed 11,822 MiB GPU peak; its report retains stage/RAM
measurements and component hashes. No installed application checkout or service was deployed.

Validation: 317 focused implementation/documentation checks passed with one optional skip;
19 additional source/docs browser checks passed. API/storage checks cover native video batch
inputs and live retention. Legacy Civitai/Files checks for direct `/loadcoach/hub` and `/loadcoach/files` routes, plus
an exact Files alert-class assertion, fail on the unchanged baseline too. The Files browser
transfer/delete check passed when rerun separately. Deployment keeps
`owg update; owg restart` when requested.

The specification below records the approved implementation scope.

Add local video generation with **WAN** (2.1 and 2.2), **LTX-2** (2.3 and 2.5), **HunyuanVideo 1.5**
and **MiniMax-H3**. It covers three surfaces:
- a **Video generation** page under Interact;
- a **Video** model setup page under Models;
- **Video** settings under Settings.

It reuses Mirror Wall's engine, runtime child, store, job API and page components. It does not
build a parallel stack.

Follow [agents.md](agents.md), [AGENTS.md](../AGENTS.md) (template reuse, template index) and
the [documentation plan](documentation-plan.md) (guides, coverage, one home per fact). Deploy
only when asked, with `owg update; owg restart`. The runtime rebuild uses the existing
`scripts/install-sdcpp.sh` route.

---

## 1. What exists and what upstream offers

Facts were checked on 2026-11-14 against the pinned stable-diffusion.cpp source
(`95ff7623…`, 2026-10-24, `~/openWeightGym/runtimes/sdcpp/source/`), upstream `master`
`examples/server/api.md`, and this repository.

| Fact | Consequence |
|---|---|
| The pinned sdcpp revision documents all four families: `docs/wan.md`, `docs/ltx2.md`, `docs/hunyuan_video.md`, `docs/minimax_h3.md`. | **No new engine and no pin bump** are needed for the first release. The engine stays `sdcpp`. |
| **MiniMax-H3 is open weights** (GGUF at `leejet/MiniMax-H3-GGUF`, safetensors at `Comfy-Org/MiniMax-H3`). It generates video *and* stereo audio together. | MiniMax runs locally like the others. MiniMax's hosted Hailuo API is a separate question (decision V8). |
| `sd-server` has an async `POST /sdcpp/v1/vid_gen` that uses the same `/sdcpp/v1/jobs/{id}` poll and cancel calls Mirror Wall already uses for `img_gen` ([runtime.py](../mirrorwall/runtime.py)). | The runtime child gains a video path beside its image path. Polling, previews, cancellation and cleanup are reused. |
| `vid_gen` inputs: `prompt`, `negative_prompt`, `width`, `height`, `seed`, `video_frames`, `fps`, `init_image`, `end_image`, `ref_images` (MiniMax-H3 only), `control_frames` (VACE), `vace_strength`, `moe_boundary`, `high_noise_sample_params`, `sample_params`, `lora`, `vae_tiling_params`, `preview`, `output_format`, `output_compression`. | These define the request contract. Mirror Wall's own request schema maps onto them, and native fields are never passed straight through. |
| **There are no audio inputs** on `vid_gen`, at the pin or on upstream `master`. WAN 2.2 S2V (audio-driven) and MiniMax-H3 reference video and audio only work in `sd-cli`. | S2V and audio/video references are deferred (decision V6). |
| Output is one container: `webm` (default), `webp` (animated) or `avi` (MJPEG). Generated audio (LTX-2, MiniMax-H3) is muxed into WebM as **16-bit PCM** (`A_PCM/INT/LIT`). | Browser playback of PCM-in-WebM is not reliable across browsers, so it needs qualification. A control-node remux to Opus may be needed (decision V2). |
| **Our build disables WebM and WebP** (`-DSD_WEBP=OFF -DSD_WEBM=OFF` in [install.py](../mirrorwall/install.py)). Only MJPEG AVI is possible today, and browsers do not play it. The libwebm and libwebp sources are already in the pinned tree's `thirdparty/`. | M1 must rebuild with both formats on. The release identity is `revision-backend-patch`, so build options have to join it or the rebuild would collide with the current release. |
| The server snaps frame counts to `4n+1`. MiniMax-H3 snaps up to `17k+5`, fixes 24 fps and aligns sizes to 32. WAN S2V is fixed at 16 fps. | The UI asks for **seconds** and shows the frame count and length that will actually be produced. |
| `GET /sdcpp/v1/capabilities` reports `supported_modes` and per-mode `features`: `init_image`, `end_image`, `control_frames`, `vace`, `high_noise`, `ref_images`, `lora`, `vae_tiling`, `preview`, `flow_shift`. | **Mode availability comes from the loaded runtime's capabilities**, recorded on each profile. It is not a hand-kept per-family table. |
| Hardware: 2 × RTX 5060 Ti 16 GB, 30 GB host RAM. `ffmpeg` n9.0.2 is installed on this machine. | WAN 2.2 A14B, LTX-2 22B and MiniMax-H3 (Qwen3-VL-32B encoder) need quantized weights plus offload or layer split, and must be qualified one by one. MiniMax-H3 may not fit. |
| Mirror Wall already provides `sdcpp` profiles, header-based component detection ([components.py](../mirrorwall/components.py)), the private asset store ([store.py](../mirrorwall/store.py), 128 MiB output cap), control-node jobs on the batch executor ([front.py](../mirrorwall/front.py)), the Model studio and the Canvas studio. | Video extends each of these in place. |
| Platform switches live in `SERVICE_SETTINGS` ([platform_services.py](../loadcoach/platform_services.py)). Navigation lives in `NAV_SECTIONS` *and* the compact group lists in [navigation.py](../weightroom/navigation.py). | Add `allow_video_generation`, and update both navigation structures. |

## 2. Families and modes

Each profile records its modes from runtime capabilities at qualification. The table below is the
**expected** result, taken from upstream docs, for planning only.

| Family (profile `family`) | Text → video | Image → video | First + last frame | Reference images | Control video (VACE) | Soundtrack | Notes |
|---|---|---|---|---|---|---|---|
| `wan2.1` (T2V 1.3B/14B, I2V 14B, FLF2V 14B, VACE 1.3B/14B) | T2V, VACE | I2V | FLF2V | VACE `-i` | VACE | — | I2V and FLF2V need `clip_vision_h`. Wan 2.1 VAE. 1.3B is the smoke-test model. |
| `wan2.2` (TI2V 5B; T2V/I2V A14B; VACE-Fun A14B) | ✓ | ✓ | I2V A14B | VACE-Fun | VACE-Fun | — | A14B is a **high/low-noise pair** (`high_noise_diffusion_model`, separate high-noise steps and CFG). TI2V 5B uses the Wan 2.2 VAE. Lightning LoRAs allow 4 steps. S2V deferred (V6). |
| `ltx2` (2.3 and 2.5, 22B) | ✓ | ✓ | 2.3 | — | — | ✓ | Gemma 3 12B with an embeddings connector (2.3), or the LTX Gemma 4 12B (2.5). Optional ×2 latent upscaler (`hires`). 2.5 needs the **conv** VAE. |
| `hunyuan-video-1.5` | ✓ | — at this pin | — | — | — | — | Qwen2.5-VL 7B (shared with Qwen-Image) plus ByT5 GlyphXL. fp16 weights need converting to GGUF to fit. |
| `minimax-h3` | ✓ | ✓ | ✓ | ✓ (images only) | — | ✓ | Qwen3-VL-32B text encoder, MiniMax variant. Fixed 24 fps, `17k+5` frames, sizes in multiples of 32. Reference images cannot be combined with start/end frames. |

Components are tracked once in Files under the existing image kind. A file such as Qwen2.5-VL 7B
that serves both Qwen-Image and HunyuanVideo is one Files entry, protected while any profile uses it.

---

## 3. Architecture

- **Package:** video lives in `mirrorwall/` (decision V1). New modules:
  - `video.py`: family defaults, limits, frame and duration snapping, and request normalization;
  - `video_api.py`: the OpenAI-shaped `/v1/videos` API.

  Everything else extends existing modules.
- **Engine and admission:** the engine stays `sdcpp`, with one supervised child per profile, as for images.
  - A profile gains `mode = "vid_gen"`. The child loads the profile's components and submits `/sdcpp/v1/vid_gen`.
  - Video profiles start **exclusive** on their cards and use the **batch** priority class (decision V3).
  - Each profile declares a hard `max_job_seconds`.
  - Cancellation calls the native cancel. If the job does not settle within a bound, the child is killed, and the reservation is released only after the process exits (the existing Mirror Wall rule).
- **Profile identity** adds family, mode, component hashes, the high-noise model, split layout,
  offload, VAE and temporal tiling, and the frame, size and duration bounds.
  - Measured envelopes are recorded per profile: per-card load and sampling peaks, VAE decode peak, host RSS, and seconds per output second.
  - Admission uses a measured envelope once it is recorded. Unmeasured profiles stay exclusive.
- **Progress stages:** queued → loading → encoding prompt → sampling (step n/N, plus the high-noise
  pass for WAN 2.2) → decoding → encoding container → saving. If preview frames qualify, the
  latest one is shown.
- **Requests** ([requests.py](../mirrorwall/requests.py) pattern):
  - Accept only Suite asset IDs or inline images (the existing size limits apply).
  - Never accept server paths or native arguments.
  - Refuse unsupported fields with a reason. Never drop them silently.
  - Snap seconds × fps to the family's frame grid, and report the effective frames, fps and length.
- **API:** OpenAI-shaped `POST /v1/videos`, `GET /v1/videos`, `GET /v1/videos/{id}`,
  `GET /v1/videos/{id}/content` and `DELETE /v1/videos/{id}`, through Turnstile with existing keys
  and model permissions.
  - Suite fields (`negative_prompt`, `seed`, `fps`, `frames`, `end_image`, `reference_images`, `control_video`, `steps`, `guidance`, `loras`, `soundtrack`) sit beside the OpenAI fields.
  - Check the current OpenAI Videos API shape before implementing, and document any differences.
  - Video models publish capability `video_generation`, so they never satisfy chat or image routes.
  - Jobs run on the existing control-node batch executor (`front.py` `start`/`execute_line`). No new scheduler.
- **Storage** ([store.py](../mirrorwall/store.py)):
  - Asset kind `video`, with its own output cap (default 512 MiB), a per-account quota and retention (decision V5).
  - Poster and thumbnail derivatives are generated as in V2.
  - No-log runs use temporary assets, as images do.
  - Provenance (resolved settings, seed, component hashes) is kept in metadata, not embedded in the file.
- **Usage:** record clips, output seconds, frames, resolution and GPU-seconds. Do not invent
  token usage. Usage and Requests show video jobs alongside image jobs.
- **Telemetry and Dry fit:** reuse Mirror Wall telemetry for per-stage peaks. Add video profile
  footprints to Dry fit's planning data ([fit.py](../weightroom/fit.py)).

## 4. Pages

All three pages start as **standalone proposals with sample data** (three designs each, rendered
by a preview script), following the repository's established pattern, unless decision V9 says
otherwise. The live page then uses the selected design. All of them reuse existing templates;
extend a macro instead of copying it.

### 4.1 Interact → Video generation (`/chat/video`)

Add a **Video** group to `NAV_SECTIONS['Interact']` with `/chat/video`, plus the compact list and
the active-path rules in `navigation.py`. Reuse the Canvas studio parts in
[generation_design_parts.html](../weightroom/pages/generation_design_parts.html) (inspector,
prompt, viewer, job and result templates) and [generation.css](../weightroom/static/generation.css).
Video-only behaviour goes in `mirrorwall/static/video.js`.

- **Model picker:** only permitted models with the `video_generation` capability. Each shows its modes.
- **Mode tabs**, shown only when the selected profile supports them:
  - **Text**;
  - **Start image**;
  - **Start and end images**;
  - **Reference images** (MiniMax-H3; prompt tokens `<Picture 1>`…);
  - **Control video** (VACE). The browser extracts frames from an uploaded clip at the profile's fps, through `<video>` and canvas, and uploads them as assets. No server decoding is needed.
- **Main controls:**
  - prompt and negative prompt, prefilled with the family default (WAN's upstream negative prompt);
  - length in seconds, with the effective frames, fps and length shown live;
  - resolution presets per family (for example 832×480, 480×832, 1280×720) with aspect choices;
  - seed / random seed;
  - **Soundtrack** on/off for LTX-2 and MiniMax-H3, which tells the prompt to describe sound.
- **Advanced:**
  - steps, guidance, sampler and scheduler, flow shift;
  - high-noise steps and guidance plus the MoE boundary (WAN 2.2 A14B);
  - LoRAs with strength (the existing LoRA template);
  - ×2 latent upscale (LTX-2);
  - VACE strength;
  - VAE and temporal tiling, set automatically by the profile but overridable within its limits.
- **Run:**
  - **Generate** and **Stop**;
  - stage progress with step counts and an estimated time from the profile's measured seconds per output second;
  - the queue position;
  - the latest preview frame.

  The form and earlier results stay visible on failure. Per-model drafts persist across model switches, as in the image studio.
- **Viewer:**
  - a `<video>` player with loop, mute and frame step (←/→);
  - fit or actual size, and download;
  - result details (resolved settings, seed, effective frames and fps), with **Reuse settings**;
  - **Use last frame as start image**, which captures the frame in the browser and stores it as an asset. This chains clips without a server-side extend mode.
- **Library:**
  - the private gallery uses poster thumbnails, filters by model, mode and date, and sorts;
  - selection is safe across polling, and deletion uses the shared two-click confirmation.

  It shares the gallery and store with images through `kind=video`.
- **States to cover:**
  - runtime missing, or built without WebM;
  - no video models, or no permitted models;
  - the account's quota is full;
  - model error, timeout, and cancelled;
  - narrow layout, and light and dark themes.

### 4.2 Models → Video (`/models/video`)

Add `/models/video` to `NAV_SECTIONS['Models']` Catalogue and the compact list. **Parameterize the
existing Images Model studio** ([mirrorwall_models.html](../mirrorwall/pages/mirrorwall_models.html),
[mirrorwall_model_setup.html](../mirrorwall/pages/mirrorwall_model_setup.html),
[image_design_parts.html](../weightroom/pages/image_design_parts.html),
[models.js](../mirrorwall/static/models.js)) by media kind, rather than copying it.

- **Navigator:** nodes and video models, with readiness (complete components, qualified profile, runtime WebM support).
- **Setup dialog:**
  1. Pick component files from Files. [components.py](../mirrorwall/components.py) gains header detection for the WAN, LTX-2, HunyuanVideo and MiniMax-H3 transformers, VAEs, the audio VAE, the LTX connector, ByT5, clip_vision and the latent upscaler.
  2. The family and variant are inferred and can be confirmed.
  3. `needs(family, variant)` lists any missing slots: diffusion model, high-noise model, VAE, audio VAE, text encoder (`t5xxl` umt5 / `llm` Qwen2.5-VL, Gemma or Qwen3-VL), `embeddings_connectors`, ByT5, `clip_vision`, latent upscaler, and optional TAE preview.

  Mismatches are refused with a reason, for example the LTX-2.5 non-conv VAE, the Wan 2.2 VAE paired with A14B, or a stock Gemma 4 for LTX-2.5.
- **Per-node profile:**
  - GPUs, split mode (single, components, layer) and offload to CPU;
  - VAE and temporal tiling;
  - limits: maximum seconds, frames, width × height, and pixels × frames;
  - defaults, and the qualified envelope (read-only evidence).
- **Workflows:** versioned presets, using the existing immutable history. Examples: "WAN 2.2 lightning 4-step", or "LTX-2.3 ×2 upscale".
- **Downloads:** use the existing Hugging Face and Files flow. Do not pre-populate a download menu; the guide links to the upstream source repositories instead. Civitai discovery gains the video base-model filters (WAN, LTXV, Hunyuan Video) for LoRAs, which needs the image and download switches.
- **Conversion:** a HunyuanVideo fp16 → GGUF step uses `sd-cli -M convert`, run as a tracked Files operation with temporary disk and RAM budgets and recorded provenance. The same applies to the LTX-2.5 text encoder at Q8.

### 4.3 Settings

| Page | Change |
|---|---|
| **Settings → General** | `allow_video_generation` switch in `settings/defaults/weightroom.toml` and `SERVICE_SETTINGS`. It filters navigation, models, files and `/v1/videos` through `PlatformServices`. Video also requires image generation to be on (decision V7). |
| **Settings → Media → Video** (`/settings/video`, new) | Built from [settings_program.html](../weightroom/pages/settings_program.html)/[setting_row.html](../weightroom/pages/setting_row.html) with the existing save and reset behaviour. Keys (each with a defaults comment, so the generated settings reference documents it): `video_max_seconds`, `video_max_width`, `video_max_height`, `video_output_max_mib`, `video_quota_gib` (per account), `video_retention_days` (0 keeps until deleted), `video_soundtrack_remux` (V2). Groups may override the limits and quota, using the same inheritance as the extraction upload limits. The adult-preview policy stays the single `image_adult_previews` setting; the page links to it. |
| **Settings → Compute nodes** | No new control. Video uses the `sdcpp` entry in `engine_gpus`. The node page lists video profiles beside image profiles. |
| **My account → Workspace** | Optional preferences, using `account_parts.html` fields: start videos muted, and loop in the viewer. |
| **Users / Groups** | No new ability. Access is model permissions plus the group limit overrides above. |

---

## 5. Work plan

| # | Milestone | Done when |
|---|---|---|
| M0 | Owner decisions V1–V9 | Recorded in this file and in [agent-status](agent-status.md) |
| M1 | **Runtime:** enable `SD_WEBP`/`SD_WEBM`, and add a build-options digest to the release identity in `mirrorwall/install.py`. Rebuild through `scripts/install-sdcpp.sh` on compute (staged, verified, atomic, rollback kept). Record whether WebM output is available in runtime diagnostics. | A Wan 2.1 T2V 1.3B smoke run (832×480, 33 frames) returns a WebM that plays in Chrome and Firefox. Existing image profiles still pass their checks. |
| M2 | **Engine and profiles:** video families, limits and defaults (`video.py`); component detection and `needs()`; the `vid_gen` path in `runtime.py` (poll, preview, cancel, cleanup); profile identity; telemetry. | Unit tests for snapping, mode validation and refusals. The cancellation and crash cleanup test passes on GPU. |
| M3 | **Control node:** asset kind `video`, cap, quota and retention; poster derivative (V2); `/v1/videos` API on the batch executor; capability routing; platform switch; usage recording. | API contract tests, permission and switch tests, and a no-log test that leaves no retained assets. |
| M4 | **Models → Video:** 3 proposals with sample data and a preview script, owner choice, then the live page via the parameterized studio. | `test_ui_consistency.py` component audit and browser checks (narrow width, empty and error states). |
| M5 | **Interact → Video generation:** 3 proposals, owner choice, then the live page. | Browser checks, including draft retention across model switches and polling-safe library selection. |
| M6 | **Settings:** General switch, `/settings/video`, group overrides, optional My account preferences. | Settings tests. Every new key has a defaults comment. |
| M7 | **Qualification** in this order: Wan 2.1 T2V 1.3B → Wan 2.2 TI2V 5B → Wan 2.1 I2V/FLF2V 14B → Wan 2.2 A14B (Q4/Q5, lightning LoRA) → LTX-2.3 → LTX-2.5 → HunyuanVideo 1.5 (converted) → MiniMax-H3. For each profile, record per-card and RAM envelopes, seconds per output second, cancellation, browser playback (including soundtrack) and owner visual acceptance, under `data/qualification/video-*`. | Each profile is qualified, or marked not runnable with the reason shown in the UI. Layer-split use is verified per card (no silent fallback). |
| M8 | **Docs:** guides in the documentation-plan format: Web app → Video generation, Models → Video models, Administration → Video settings, and the manual-build note in [installation.md](installation.md) about `SD_WEBM`. Update the AGENTS.md template index. | The docs coverage test passes for `/chat/video`, `/models/video` and `/settings/video`. |
| Later | S2V and audio/video references (V6); server-side extend and long video; frame interpolation; a hosted MiniMax API (V8) | Separate plan when requested |

M2 and M3 can run in parallel after M1. Pages (M4–M6) need M2 and M3 contracts, but their proposals can start immediately with sample data.

**Tests to add:** `tests/test_video_requests.py`, `test_video_store.py`, `test_video_api.py`,
`test_video_pages.py` and `test_video_designs.py` (proposal fields checked against the live page,
as `test_usage_designs.py` does). Add the runtime-capability probe to the existing sdcpp installer tests.

## 6. Approved decisions

| # | Question | Recommended default |
|---|---|---|
| V1 | Should video go in `mirrorwall/` or a new package? | **`mirrorwall/`**: same engine, store, jobs and studio components, with no copied code. |
| V2 | Container and playback | Enable WebM and WebP. If PCM soundtracks don't play in Firefox or Safari, **remux the audio to Opus with ffmpeg on the control node** (video stream copied). The same optional ffmpeg makes posters. Without ffmpeg, posters come from the browser on first view, and soundtracks stay PCM with a note. |
| V3 | GPU policy while a long video job runs | **Exclusive and batch class.** It won't start while live voice holds the card, and it yields only at job boundaries. You may dedicate a card through `engine_gpus`. |
| V4 | Default limits | 5 s at 832×480 by default. Administrators can raise the limit to 10 s and 1280×720 once a profile qualifies. |
| V5 | Storage | 512 MiB per file, a 10 GiB quota per account, kept until deleted. Group overrides are allowed. |
| V6 | S2V and audio/video references (not on the server API) | **Wait for upstream.** Revisit when the server gains audio inputs (recipe flags `ref_audios` and `ref_video_audios` suggest it is coming). Avoid a second local patch. |
| V7 | A separate platform switch? | **Yes:** `allow_video_generation`, which also requires image generation to be on. |
| V8 | MiniMax's hosted Hailuo API | **Out of scope.** MiniMax-H3 runs locally, and Cloud APIs only speaks OpenAI's format. |
| V9 | Proposals before the live pages? | **Yes, three designs each** for Interact and Models, as for every other recent page. Settings goes straight to live, using the shared settings templates. |
