<!--docs
title = "Usage and costs"
summary = "Review scoped activity, token totals and recorded or estimated costs."
order = 10
covers = ["/usage"]
-->
# Usage and costs

Review scoped activity, token totals and recorded or estimated costs.

## Do it

1. Open **Usage**, set **Date range**, and narrow **Model**, **Source**, **Outcome**, **Level** or compute node.
2. Select **Apply**. Compare **Over time**, **By model** or another chart view with the report tables.
3. Inspect the cost categories and **Missing counts** before interpreting a total.

## Options

**Recorded USD**, **Model-rate USD** and **Mock fallback USD** distinguish the cost basis. **Mock input rate / M tokens** and its output counterpart change fallback estimates; they do not rewrite recorded charges. **Reset** returns to the default filters.

Video work adds a resource band for completed clips, output seconds, frames and GPU-seconds.
GPU-seconds count reserved cards multiplied by generation elapsed time. Video does not report
language tokens; clip resolution and resolved settings remain in its private result and request
metadata. The same owner, model, date and outcome filters apply.

## Limits and troubleshooting

The account's reporting permissions scope every result. Missing usage is not zero usage. Estimated cost is not a provider invoice, and unlogged work leaves no retained usage evidence.
