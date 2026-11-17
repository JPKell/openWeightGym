<!--docs
title = "Sampler comparison"
summary = "Run one prompt through every chosen scheduler and sampler, then compare the images side by side with a shared zoom."
order = 32
covers = ["/models/images/samplers"]
-->
# Sampler comparison

Run one prompt through every scheduler and sampler you choose. Each scheduler is tried with every
sampler, all on the same seed, so only the sampler and scheduler change between images.

## Before you start

Use an account with the [manage-models](/docs/system/abilities#ability-manage-models) ability and
at least one installed image model. A comparison is an ordinary image job. It shows under
**Interact → Image generation → Queue** and its images are saved to your private library.

## Do it

1. Open **Models → Sampler comparison**.
2. Choose the image model and enter the prompt, plus a negative prompt if the model supports one.
3. Set the width, height, steps, CFG scale (or distilled guidance for Flux-style models) and seed.
   **Images per combination** above 1 adds the next seeds, so each combination gets the same
   set of seeds.
4. In **Schedulers** and **Samplers**, untick the ones you do not want. **All** and **None** set
   the whole list at once. The count under **Run comparison** shows how many images the run makes.
5. Select **Run comparison**. The results fill in as each image finishes. **Pause** and **Stop**
   act on the whole run.

## Compare results

Results are grouped by scheduler. Each image's sampler and scheduler are shown under it, followed by
its seed, steps and CFG. A combination the runtime refuses is marked as failed, with the reason,
and the rest of the run continues.

- **Zoom** enlarges every image around the same point. Move the pointer over any image to choose
  the point. You can also focus an image and pan with the arrow keys, zoom with `+` and `-`, or
  hold Ctrl and use the mouse wheel. Above 1× the full-resolution images are loaded.
- Tick the box under an image to select it, then turn on **Selected only** to compare just those.
- **Tile size** changes how many images fit in a row.
- **Earlier comparisons** reopens a previous run, including one that is still running.

## Limits

A comparison can make up to 512 images: schedulers × samplers × images per combination. Other
image jobs stay limited to 128. The usual image limits for the model (pixel budget and steps)
still apply. A shared-residency model whose profile was measured at CFG 1 keeps CFG at 1. Use an
exclusive model entry to compare other CFG values.
