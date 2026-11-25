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
   previous version. **Use settings as draft** restores earlier settings for review. Workflow removal follows the [shared workflow rules](/docs/model-guides/image-models#options).

## Components and sources

Download weights through the normal Hugging Face/Files flow. There is no pre-populated video
download menu. Shared encoders remain one Files entry and are protected while any profile uses them.

| Family | Native component instructions |
|---|---|
| WAN | [WAN 2.1 and 2.2](https://github.com/leejet/stable-diffusion.cpp/blob/master/docs/wan.md) |
| LTX-2 | [LTX-2.3/2.5](https://github.com/leejet/stable-diffusion.cpp/blob/master/docs/ltx2.md) |
| HunyuanVideo | [HunyuanVideo 1.5](https://github.com/leejet/stable-diffusion.cpp/blob/master/docs/hunyuan_video.md) |
| MiniMax | [MiniMax-H3](https://github.com/leejet/stable-diffusion.cpp/blob/master/docs/minimax_h3.md) |

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

Each session stages its components in `engine-scratch` inside the compute node's data folder.
They are hard-linked when that folder shares a filesystem with the weights, and otherwise copied,
which needs free disk space for the whole bundle. The system temporary folder is not used,
because it may be a small memory-backed filesystem. Folders left by a LoadCoach that was killed
are removed before its next session starts.
Missing weights, incompatible components or insufficient memory mean a profile is not runnable;
the editor and generation page show the reason. Configuration saves are not GPU qualification.

For repeatable node checks, `scripts/qualify-video.py --help` documents the runtime, weights,
profile, node key file and control-node benchmark host arguments. It requires an idle node and
empty benchmark queue, takes the existing external-work lease, caps host memory, verifies
component hashes and records smoke, cancellation, crash and Chrome/Firefox playback evidence
under the requested `data/qualification/video-*` directory. It does not deploy the application.
Owner visual acceptance is recorded separately.

After a deployment, `scripts/smoke-video.py --url CONSOLE --key-file KEY` checks every video
model the key may use. It fails a model whose qualification failed on the current build, makes a
short text clip and a start-image clip through `POST /v1/videos`, checks each WebM and deletes
it. It changes no settings and wakes no node.

### When the runtime changes {#when-the-runtime-changes}

Evidence belongs to the runtime build: the `sd-server` and `sd-cli` programs plus the workflow
contract version of the application. An application update that leaves both unchanged keeps
every model's capabilities and memory evidence. Evidence recorded before this rule carries over
when the programs have not changed since it was recorded.

After the programs are rebuilt, or an update raises the contract version, each model keeps
offering what the earlier build proved. The model studio says the runtime changed, and Video
generation notes that earlier evidence is in use until the model is re-qualified. While
**Re-qualify image and video models when idle** is on (Compute nodes → Power and idle, on by
default), the node re-qualifies each such model once per build. It runs at the lowest priority
and only when nothing is queued or running. It may unload idle models, as any load does, and
never wakes a node. Only the output's SHA-256 and three small frames are kept; the studio shows
them with the run's time. A failed run marks the model unqualified on that build and opens an
operational alert in Alerts. It is not retried by itself; **Run qualification clip** still
re-qualifies the model.

The optional runtime is rebuilt through `scripts/install-sdcpp.sh`; application deployment keeps
the established `owg update; owg restart` route. See [Installation](/docs/operations/installation).

## Related

[Video generation](/docs/web-app/video-generation) · [Files](/docs/model-guides/files) ·
[Video settings](/docs/administration/video-settings)

[Shared form controls and confirmations](/docs/getting-started/navigation#forms-and-confirmations)
