<!--docs
title = "Roles and routing"
summary = "Give clients a stable role name for eligible model selection."
order = 60
covers = ["/roles"]
-->
# Roles and routing

Give clients a stable role name for eligible model selection.

## Before you start

Use an account with [model management](/docs/system/abilities#ability-manage-models). Disabled platform services may hide media and download choices.

## Do it

1. Open **Roles**, create or select a role, and review **Which models can answer?**
2. Use **Add a candidate**, set its **Weight**, and select **Save**. A zero weight pauses a candidate; an empty weight removes it.
3. Inspect **See what gets picked** and **Behind the pick** using the saved settings. Clients send the role name in their model field.

## Options

**Fine-tune selection** holds optional requirements, benchmark preferences and loaded-model bonuses. The request modes **Best**, **Available** and **Fast** emphasize base preference, readiness or time estimates. An explicit model name still selects that model.

## Limits and troubleshooting

The preview uses saved settings; unsaved changes do not alter it. Actual admission also checks model permission, context, images, tools and sensitivity. **No eligible model** needs a candidate or requirement/access correction. A timing estimate is not a response-time guarantee.

## Related

[Model catalogue](/docs/model-guides/catalogue) · [Cloud APIs](/docs/model-guides/cloud-apis)
