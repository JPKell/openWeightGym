<!--docs
title = "System logs"
summary = "Read and inspect journal output from the selected node and source."
order = 80
covers = ["/output"]
-->
# System logs

Read and inspect journal output from the selected node and source.

## Do it

1. Open **System logs** and choose the compute node, log source, time range and line limit.
2. Search the returned entries and select one to inspect its original text and nearby context.
3. Export the selected result when needed. Any offered local-model analysis is a separate inference request.

## Limits and troubleshooting

This page requires [service-output access](/docs/system/abilities#ability-restart-services). Inferred log levels are presentation hints, not authoritative application outcomes. Offline snapshots are private to the account and exact filter; reconnect for fresh journal evidence. Missing output can mean an unavailable node or source.
