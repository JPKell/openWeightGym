<!--docs
title = "Video generation"
summary = "Generate short local clips, review private results and chain clips with captured frames."
order = 36
covers = ["/chat/video"]
-->
# Video generation

Create short clips with a permitted local video model. The Canvas layout keeps the prompt,
controls, player, queue and private library together.

## Before you start

An administrator must configure and qualify a [video model](/docs/model-guides/video-models).
Both image and video platform services must be enabled. Missing runtime capabilities or WebM
support disables generation; saving a model alone does not qualify it.

## Do it

1. Open **Interact → Video generation**, choose a model and write **Prompt**.
2. Choose the length, resolution and an offered mode. The effective frame count, frame rate and
   length appear below the controls; frame grids can change the requested length.
3. Select **Generate**. Runs use the existing batch executor and exclusive GPU admission.
   **Stop** cancels the active run; it waits for the owned runtime to stop before releasing its GPU.
4. Select a completed clip in **Your clips**. Use the player, frame arrows, **Download** or
   **Reuse settings**. Unsent per-model drafts, including the chosen resolution, stay in this tab while switching models and as runs finish.
   **Load earlier clips** pages through retained history; model, mode, date and sort controls
   apply to the loaded clips.

## Options

Start/end images, ordered reference images and control video appear only for supported profiles.
Control clips are sampled into frames in the browser. References are private assets; remote URLs,
server paths, audio inputs and arbitrary native arguments are refused.

**Use last frame as start image** captures a completed clip's final frame in the browser, for a
model offering start-image conditioning. This creates a new clip with its own settings and seed.

Soundtrack controls require audio components. Describe the desired sound in the prompt.
Optional control-node ffmpeg strips disabled sound, remuxes enabled sound to Opus and generates
posters. Without it, native PCM audio needs a compatible player; sound cannot be disabled when
the native model always produces audio. **Loop** and **Muted** affect playback only.

**Temporary · no retained history** requires the existing no-log permission and appears only while the compute policy allows no-log work. Inputs and results
remain in bounded temporary memory; temporary input assets require a temporary job. Stop cancels running work; results expire from memory after one hour. No durable batch or
asset rows are created.

## Limits and troubleshooting

The effective account/group limits and profile limits both apply. Shorten the clip or lower its
resolution after a limit error. A full quota requires deleting clips or raising the account limit.
Errors, timeouts and cancellation preserve the form and earlier clips. Deletion requires two
clicks; stop an unfinished job before deleting it.

LTX latent upscale is offered only when the loaded server advertises support. Audio-driven WAN
S2V, audio/video references, server-side extension and interpolation are not supported.

## API

The local `/v1/videos` resource queues work, with owner-scoped list, retrieve, content and delete
operations. See the [generated API reference](/docs/api) for routes. The shape follows the
[historical OpenAI Videos API](https://developers.openai.com/api/reference/resources/videos),
which OpenAI retired on September 24, 2026; this installation performs local inference.

Suite differences: JSON requests, configured local model names, WebM output, fractional seconds,
profile-bounded dimensions/fps/frames, inline images or owned asset IDs, explicit local sampling
extensions, temporary no-log jobs and a cancellation operation. Content supports byte ranges
and `variant=thumbnail`. Sora models, hosted inference, remix/edit/extend and remote input URLs
are not implemented. The `/v1/videos/generations` transport serves the batch executor's admitted
render requests; it is not the asynchronous create operation.

For example, send this JSON to `POST /v1/videos` with your usual API key and an available model
name, then poll the returned ID. Download `/v1/videos/{id}/content` after status is `completed`.

```json
{"model":"wan21-small","prompt":"A red sailboat moving across a calm lake.","seconds":2.1,"size":"832x480","seed":42}
```

## Related

[Video settings](/docs/administration/video-settings) · [Video models](/docs/model-guides/video-models)
