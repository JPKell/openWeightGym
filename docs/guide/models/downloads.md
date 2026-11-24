<!--docs
title = "Downloads"
summary = "Find source files and queue storage-only downloads."
order = 100
covers = ["/hub", "/civitai"]
-->
# Downloads

Find source files and queue storage-only downloads.

## Before you start

Use an account with [model management](/docs/system/abilities#ability-manage-models). Disabled platform services may hide media and download choices.

## Do it

1. Open **Hugging Face**, search models or enter `owner/name`, then select the required file or quantization. Split weights are selected as complete sets.
2. Choose an available storage destination and start the offered download action. Review the download queue's state and errors.
3. For image and video sources, **Civitai** offers its supported model/version search. Install a downloaded file through the relevant model editor after the transfer completes.

## Civitai filters

Use **Base model** to narrow searches by family. Choices include Wan video variants, MiniMax H3, MOSS Image and the Flux Dev, Schnell, Krea, Kontext and Klein variants. Video families also include Hunyuan Video, Mochi, CogVideoX and LTXV. Changing the base model starts a fresh provider search; **All base models** clears that restriction.

**Model type** covers Civitai's image and video asset types, including checkpoints, diffusion weights (UNet), motion modules, text encoders, VAEs, CLIP/CLIP vision, LoRA/LoCon/DoRA adapters and ControlNet. Combine a component type with a video base model to find its parts, or use **All types** to include every category. **File format** applies when choosing a file.

These choices filter downloads. Runtime compatibility is checked separately when configuring a model or workflow.

## Civitai previews

Open **My account → Workspace → Civitai previews**, select **Enable adult previews**, then **Save preview preferences**. This choice applies only to your account and is available when you can use Civitai and the administrator permits adult previews. Existing choices are retained. Search Civitai again after saving to refresh previews.

## Limits and troubleshooting

Downloads require the platform download switch; Civitai also requires image generation. Provider credentials belong to the destination node and can be managed under **Settings → Downloads**. Current concurrency limits apply to new transfer admission; active transfers finish when a limit is lowered. Downloads go into the destination's configured model storage; a node without one offers no destination.

## Related

[Files](/docs/model-guides/files) · [Model catalogue](/docs/model-guides/catalogue)
