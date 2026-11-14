<!--docs
title = "Compute nodes"
summary = "Inspect and configure the exact node that owns a model or service."
order = 20
covers = ["/compute-nodes"]
-->
# Compute nodes

Inspect and configure the exact node that owns a model or service.

## Do it

1. Open **Compute nodes** and use **Find a node…** or the status filter.
2. Select the intended node and review its current settings, GPU/engine assignments and Wake policy.
3. Save only the fields intended for that node. Inspect pending/restart evidence separately from the configured value.

## Options

A permitted superuser can use **+ Add compute node → Make a pairing code**. Follow the command shown by the existing installation workflow; [Installation](/docs/operations/installation) is its maintained operating guide.

## Limits and troubleshooting

A node being listed does not establish that it is answering or accepting work. Wake overrides are owned on the control node; connection listeners and key paths are read-only evidence. Retired nodes remain distinguishable from configured active nodes. Do not treat a successful settings save as proof of a running service change.
