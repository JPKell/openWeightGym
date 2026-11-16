<!--docs
title = "Batches"
summary = "Inspect durable batch progress, results and exact lifecycle history."
order = 40
covers = ["/batches"]
-->
# Batches

Inspect durable batch progress, results and exact lifecycle history.

## Do it

1. Open **Batches** and filter or sort the ledger.
2. Select a batch to inspect processed, successful and error counts.
3. Use the available answer/error download links, or **View history** for its exact Event log context.

## Limits and troubleshooting

Downloads remain owner-only and expire with configured file retention. **Cancel** confirms before stopping remaining work; completed results stay available. If event history is missing, the saved batch record can still explain its state. Submitted API batches are described in [API reference](/docs/api).

Image batches follow the job ordering described in [Image generation](/docs/web-app/image-generation#review-images-and-runs).
