<!--docs
title = "Video models"
summary = "Configure local video components and versioned workflows with explicit GPU and runtime limits."
order = 31
covers = ["/models/video"]
-->
# Video models

Configure WAN 2.1/2.2, LTX-2.3/2.5, HunyuanVideo 1.5 and MiniMax-H3 through the shared model studio.
Video uses the existing stable-diffusion.cpp engine, Files library and workflow history.

## Do it

1. With [model-management access](/docs/system/abilities#ability-manage-models), open **Models → Video**
   and choose **Add video model**. Select destination nodes and diffusion weights from Files.
2. Confirm the family and complete the header-based component checklist. It explains missing
   or incompatible parts. WAN A14B requires separate low/high-noise diffusion weights; TI2V 5B
   uses the Wan 2.2 VAE, while other WAN variants use the Wan 2.1 VAE.
   The automatic upstream recipe follows the detected variant. Choose an explicit recipe for
   distilled LTX or MiniMax reference-image weights. Loaded recipe declarations determine modes;
   command-line loading's all-features advertisement is not used for video.
3. Choose GPUs, split strategy, host RAM cap, staging and bounds. Video always reserves its GPUs
   exclusively. Do not raise bounds beyond a measured profile envelope.
4. Save the model. Create named workflows beside it; saving another version preserves the
   previous version. **Use settings as draft** restores earlier settings for review.

## Components and sources

Download weights through the normal Hugging Face/Files flow. There is no pre-populated video
download menu. Shared encoders remain one Files entry and are protected while any profile uses them.

| Family | Native component instructions |
|---|---|
| WAN | [WAN 2.1 and 2.2](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/wan.md) |
| LTX-2 | [LTX-2.3/2.5](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/ltx2.md) |
| HunyuanVideo | [HunyuanVideo 1.5](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/hunyuan_video.md) |
| MiniMax | [MiniMax-H3](https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/minimax_h3.md) |

LTX-2.5 requires its **conv** video VAE and its LTX-tuned Gemma 4 encoder with bundled projection.
Stock Gemma 4 and the diffusion-decoder VAE are incompatible. HunyuanVideo shares Qwen2.5-VL 7B
with Qwen Image and also needs ByT5 GlyphXL. WAN 2.1 I2V/FLF2V needs CLIP Vision H.
Unknown headers remain unrecognized rather than being guessed from names.
An optional tiny video autoencoder must match the model's latent channels. Selecting one also
changes native decoding; qualify its output quality and memory separately.

For HunyuanVideo safetensors or the LTX-2.5 Gemma encoder, open its **Files** record and choose
**Convert video component** on its owning node. Enter RAM and temporary disk budgets and select
**Convert to Q8_0 GGUF**. Files shows the tracked outcome. Native conversion uses the configured
sd-cli, enforces process limits and preserves source weights; the output records hashes and
conversion provenance. Conversion success does not establish GPU fit or visual quality.

## Qualification

Runtime capabilities must be recorded for the exact component/build/profile identity before
the studio offers generation modes. Qualify short clips before larger bounds: load/sampling/VAE
GPU peaks on every assigned card, host RSS, time per output second, cancellation, process-crash
cleanup, browser playback and owner visual review. Unmeasured profiles remain exclusive.
Layer splitting must be confirmed on both cards; silent single-card fallback fails the job.
**Run qualification clip** submits a short real clip through the existing batch executor.
Inspect it under Video generation; successful runs publish observed runtime capabilities for
the current build and file revisions. The first clip is evidence for that size, not larger bounds.
Missing weights, incompatible components or insufficient memory mean a profile is not runnable;
the editor and generation page show the reason. Configuration saves are not GPU qualification.

For repeatable node checks, `scripts/qualify-video.py --help` documents the runtime, weights,
profile, node key file and control-node benchmark host arguments. It requires an idle node and
empty benchmark queue, takes the existing external-work lease, caps host memory, verifies
component hashes and records smoke, cancellation, crash and Chrome/Firefox playback evidence
under the requested `data/qualification/video-*` directory. It does not deploy the application.
Owner visual acceptance is recorded separately.

The optional runtime is rebuilt through `scripts/install-sdcpp.sh`; application deployment keeps
the established `owg update; owg restart` route. See [Installation](/docs/operations/installation).

## Related

[Video generation](/docs/web-app/video-generation) · [Files](/docs/model-guides/files) ·
[Video settings](/docs/administration/video-settings)
