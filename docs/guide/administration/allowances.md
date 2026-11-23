<!--docs
title = "Spending allowances"
summary = "Inspect recorded spending and configure the scopes you may manage."
order = 50
covers = ["/allowances"]
-->
# Spending allowances

Inspect recorded spending and configure the scopes you may manage.

## Do it

1. Open **Spending allowances** and review the displayed mode, period and USD basis.
2. Inspect **Recorded USD**, **Remaining USD**, **Unknown costs** and **Without ending** for each visible scope.
3. If allowed, edit **Allowance USD** and select **Save**. Configure the overall policy in the shared settings controls on this page.

## Limits and troubleshooting

Read [Spending policy](/docs/operations/settings-policy#spending-policy) for advisory, warning and hosted-blocking behavior. Recorded spending excludes unknown and in-flight costs; it does not reserve money or impose a provider-side billing cap. Ordinary members see their authorized scopes; management controls require current authority.

The page waits at most five seconds for nodes to send their latest records; a node that does not answer in time is left out until the next load. A recorded-spending report that takes too long answers **The allowance report took too long. Retry shortly.** instead of slowing other reports.

## Related

[Usage and costs](/docs/monitoring/usage) · [Cloud APIs](/docs/model-guides/cloud-apis)
