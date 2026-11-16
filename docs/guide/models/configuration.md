<!--docs
title = "Model configuration and presets"
summary = "Edit per-node settings, presets and placement."
order = 20
covers = []
-->
# Model configuration and presets

Edit per-node settings, presets and placement.

## Before you start

Use an account with [model management](/docs/system/abilities#ability-manage-models). Disabled platform services may hide media and download choices.

## Do it

1. Open a model from **Models** and choose **Configuration**.
2. Expand a group with **Edit**. Tick the node pills you intend to edit together, review the fields and save the form.
3. Use **Compute nodes** for placement and **Copy or move…**. Review **File & benchmarks** for file evidence and **Maintenance** for operational actions.

## Options

**Adapters → Remove from draft** removes one adapter on Save without deleting its file. **Discard changes** restores saved rows.

**Presets** reuses compatible settings. **Load preset** fills the editor; saving applies it. **Save preset** retains a named configuration. Shared presets can affect compatible quantizations; **Same file only** and **This entry only** identify narrower scopes. The target retains its own file, projector and adapters.

### Native MoE caching and host RAM

For a llama.cpp GPU model that keeps experts or other weights in host RAM, set **Hardware → Host RAM budget (MiB)** on each node. This reserves the full budget before loading and caps the whole process in the worker's existing memory controls. Include weights, context, work buffers and loading overhead; the GPU cache size alone does not describe host RAM use. A positive budget is required when native caching is enabled. With caching disabled, blank or zero retains existing admission. CPU-only models use measured RAM admission and reject a nonzero Host RAM budget.

With llama.cpp 1.0, **Advanced loading flags** can set `cpu-moe = true` (keep all expert weights in host RAM), or `n-cpu-moe` (the first N layers), together with `moe-cache-mib` (the GPU expert cache) and `moe-cache-cpu-share` (the fraction of cache misses computed on the CPU, from 0 to 1). These flags require an installed runtime that advertises them. The cache helps small decode batches; large prompt batches still upload their experts. Both host RAM and measured VRAM must fit. CUDA unified memory stays governed by the existing node policy; CPU expert placement is a separate choice.

To configure an existing llama.cpp MoE entry, select GPU placement, set its host RAM budget, then add `cpu-moe = true`, `gpu-layers = all`, a positive `moe-cache-mib`, and initially `moe-cache-cpu-share = 0`. Save and use the normal model request path. Alternatively, choose `n-cpu-moe` or an explicit expert `override-tensor` placement. A cache without host experts, a positive CPU share without a cache, invalid numeric values or a missing RAM budget is rejected before loading. Cache size is GPU memory in addition to attention, KV and work buffers; choose it to leave room for those allocations.

Compare cache sizes and CPU shares using the same model, quantization, context and prompt/output workload through OWG's existing benchmarks. Record prompt processing, time to first token, decode throughput, peak RAM/VRAM and concurrent-call behavior. Upstream's [MoE cache guide](https://github.com/ggml-org/llama.cpp/blob/v1.0.0/docs/moe-cache.md) explains hardware-dependent tuning; its standalone tuner starts servers outside OWG admission, so do not run it alongside managed workloads.

Native caching overlaps Strata's hybrid expert execution. Keep Strata available until equivalent workloads establish native performance, memory fit and output quality; matching options alone do not establish a replacement.

## Limits and troubleshooting

View switches keep one edit form. Check the selected nodes before saving. Unknown measurements do not establish fit, and changing configured context is different from loading a model with that capacity.

Without an explicit `parallel` setting, OWG launches llama-server with one slot, matching its one-request admission default. Explicit counts and `-1` (server auto) remain available in **Context and requests**.

A positive host RAM budget requires delegated cgroup-v2 memory controls with the worker's existing finite `MemoryMax` and `MemorySwapMax=0`. If unavailable, loading fails before the model starts. Use the established `owg update; owg restart` route to install current service definitions; manual service requirements appear in the error. A too-small budget can terminate the child process and must be remeasured after adjustment.

## Related

[Dry fit](/docs/model-guides/dry-fit) · [Fingerprints](/docs/model-guides/fingerprints)
