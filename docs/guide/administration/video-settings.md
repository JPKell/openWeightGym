<!--docs
title = "Video settings"
summary = "Control local video availability, effective limits, private storage and soundtrack playback."
order = 41
covers = ["/settings/video"]
-->
# Video settings

Use **Settings → General → Platform services → Video generation** to enable local video.
Image generation must also be enabled. Both switches apply to navigation, model discovery
and API admission; disabling video does not grant access through a bookmarked route.

## Do it

1. With settings-management access, open **Settings → Media → Video settings**.
2. Adjust clip length, landscape dimensions, output size, per-account quota, retention or
   Opus soundtrack remux. Save using the normal settings forms; reset resumes the file/default.
3. Under **Groups**, leave a video limit blank to inherit the global value, or save an override.
   The model profile retains its own hard bounds even for a superuser group.

The [generated Settings reference](/docs/operations/settings-reference) documents each key,
shipped default and scope. Portrait presets use the same longest side and pixel-area budget.
Retention zero keeps clips until deletion. Pruning protects assets referenced by active jobs.
Quota enforcement happens when retaining a completed output, without deleting earlier clips.

## Playback and policy

Optional ffmpeg on the control node produces posters and remuxes PCM audio to Opus while copying
the video stream. Without it, the browser can provide a poster and soundtrack playback depends
on the player. Generated prompts, seeds and component provenance stay in private metadata.

Video uses the existing `sdcpp` GPU assignment under **Compute nodes**. There is no separate
GPU scheduler or new ability: current model permissions and the existing no-log permission apply.
Adult source-preview policy remains on [Image settings](/docs/administration/media);
there is no separate video adult-preview switch.

## Related

[Video models](/docs/model-guides/video-models) · [Video generation](/docs/web-app/video-generation)
