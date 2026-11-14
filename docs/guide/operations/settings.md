<!--docs
title = "Settings, data and privacy"
summary = "Understand file defaults, saved overrides and private retained data."
order = 40
covers = []
-->
# Settings, data and privacy

Settings combine deployment files with validated saved overrides.

## Where settings come from

Committed `settings/defaults/NAME.toml` is the base, host-local `settings/NAME.toml` overrides it, and saved Web changes override applicable site-editable fields. The [settings reference](/docs/operations/settings-reference) shows keys, defaults and meanings without reading live deployment files.

Use [General settings](/docs/administration/settings) for search, changes, restart notices, history and override transfer. [Settings policies and restarts](/docs/operations/settings-policy) owns live/restart behavior, credential controls, sign-in and notification contracts. [Compute nodes](/docs/administration/compute-nodes) owns node administration.

## Data and privacy

Host settings, `keys/` and runtime `data/` are private. Accounts/application history belong to the control node; model files/runtime state belong to their compute nodes. [Stores and ownership](/docs/data/stores) and [Backups](/docs/data/backups) describe recovery boundaries.

Collection/workflow sources are owner-scoped. Capture, conversation expiry and backup schedules follow saved policy. Permitted no-log work is excluded from request history; it does not erase earlier records. Hosted access and spending checks apply; sensitivity can prevent offload. A cost estimate differs from recorded cost, and an advisory allowance is not a reservation.

See [My account](/docs/web-app/my-account) for personal privacy controls and [Allowances](/docs/administration/allowances) for spending policy.
