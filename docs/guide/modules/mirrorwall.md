<!--docs
title = "Mirror Wall"
summary = "Image runtime and private library components."
order = 80
covers = []
aliases = ["/docs/mirrorwall"]
code = "mirrorwall"
-->
# Mirror Wall

Use the feature guides for current image generation and model configuration.
[Image generation](/docs/web-app/image-generation) explains the canvas, private library and queue.

## How an image is made {#workflow}

Read the [image runtime code](/docs/code/mirrorwall).

## Models and their parts {#models}

See [Image models](/docs/model-guides/image-models) for components and compatibility.

## Adding a model {#adding}

See [Image model setup](/docs/model-guides/image-models#do-it).

## Reference images {#references}

See [Image generation](/docs/web-app/image-generation).

## Resize and add detail {#resize}

See [Resize and add detail](/docs/web-app/image-generation#resize).

## Workflow contract {#contract}

Image and video qualification evidence is bound to the native programs and
`WORKFLOW_CONTRACT` in `mirrorwall/engine.py`. Raise the contract when a change alters what a
qualification run measured or proved: component staging, VAE or temporal tiling, the native
payload or command line, a memory-relevant default or limit, the envelope or the metering. Every
node then re-qualifies its image and video models when idle. Changes that do not affect evidence
leave it as it is. `tests/test_qualification.py` pins a hash of that code and fails until the
contract is raised or the pin is renewed.

## When something goes wrong {#trouble}

See [Image generation troubleshooting](/docs/web-app/image-generation#limits-and-troubleshooting).
