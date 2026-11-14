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
3. For image sources, **Civitai** offers its supported model/version search. Install a downloaded file through the relevant model editor after the transfer completes.

## Limits and troubleshooting

Downloads require the platform download switch; Civitai also requires image generation. Provider credentials belong to the destination node and can be managed under **Settings → Downloads**. Current concurrency limits apply to new transfer admission; active transfers finish when a limit is lowered. Downloads do not directly populate the active model cache.

## Related

[Files](/docs/model-guides/files) · [Model catalogue](/docs/model-guides/catalogue)
