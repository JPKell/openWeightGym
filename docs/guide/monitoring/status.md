<!--docs
title = "Status"
summary = "Inspect live request lanes and node capacity before taking an operational action."
order = 90
covers = ["/status"]
-->
# Status

Inspect live request lanes and node capacity before taking an operational action.

## Do it

1. Open **Status** and inspect the request lanes, active batch groups and node capacity ledger.
2. Follow a visible request link to its diagnostics rather than inferring a failure from queue position.
3. Review the exact node and confirmation prompt before using an offered control.

## Limits and troubleshooting

The page scopes caller details by permission. An unavailable node has unknown current capacity. Held or locked models and active work have separate protections; a model can remain resident while idle. Controls are shown only when the current account and node allow them.
