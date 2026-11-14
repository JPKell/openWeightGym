<!--docs
title = "Node measurements"
summary = "Compare retained resource measurements across models and compute nodes."
order = 40
covers = ["/benchmarks/node-measurements"]
-->
# Node measurements

Compare saved resource measurements by model and compute node.

## Before you start

Run a resource benchmark from [Runs](/docs/benchmarks/runs). This matrix reads saved evidence; displaying it does not load a model.

## Do it

1. Open **Benchmark → Node measurements**. Choose the model type and resource benchmark.
2. Choose **Measured values** or **Benchmark scores** under **Display**. Select a node column to sort its values.
3. Open a saved value to inspect its score, settings and evidence before comparing machines.

## Limits and troubleshooting

**Settings changed** and **Rerun recommended** qualify evidence. **Not run**, **Failed**, **Not on node** and **Inventory unavailable** describe different gaps; none means zero resource use. Unavailable inventory cannot establish that a model is absent.

Scores depend on the saved formula. Use measured values for underlying quantities and [Manage scoring](/docs/benchmarks/manage) for their interpretation.

## Related

[What is measured](/docs/benchmarks/measures), [Compare](/docs/benchmarks/compare), [Telemetry](/docs/monitoring/telemetry).
