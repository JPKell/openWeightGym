<!--docs
title = "Dry fit"
summary = "Plan combined memory use and review named model sets."
order = 70
covers = ["/dry-fit"]
-->
# Dry fit

Plan combined memory use and review named model sets.

## Before you start

Use an account with [model management](/docs/system/abilities#ability-manage-models). Disabled platform services may hide media and download choices.

## Do it

1. Open **Dry fit** and choose a compute node. Search its catalogue and select the models to plan together.
2. Adjust LLM contexts. **Override limits** allows a custom positive context; **Lock** protects a value from **Fit unlocked contexts**.
3. **Save current selection** retains a named set. Use **Preview** before applying its settings or requesting the complete set.

## Options

VRAM and system RAM are separate budgets. Image/audio profiles have fixed resource costs. **Apply settings** changes configuration without inference. **Apply, load and hold 10 minutes** additionally requests the reviewed set; its admission checks still protect active work and independent holds.

## Limits and troubleshooting

An offline node has unknown capacity. An estimated fit is not a successful load. Saved definitions and guarded undo survive restart, while runtime holds do not restart models. Deleting a saved selection leaves already applied settings and existing holds in force.

## Related

[Model configuration](/docs/model-guides/configuration)
