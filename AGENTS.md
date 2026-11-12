# Repository instructions

## Web UI

Before creating or changing a web element, search the templates below and their callers.
Reuse the matching macro, include or DOM `<template>`; extend it when necessary instead of
copying its markup. When a meaningful component repeats without a template, extract one and
update all matching uses, including fragments and JavaScript-generated content.

Preserve form names, IDs, HTMX targets, data attributes, escaping and accessibility behavior.
Use the existing CSS tokens and Bootstrap primitives for ordinary tags; do not introduce
one-off styles or wrappers around every HTML tag. Keep unique diagrams and standalone clients
within their own layouts.

### Template index

| Element | Canonical source |
|---|---|
| Page shell, navigation, common assets and DOM templates | [frame.html](weightroom/pages/frame.html) |
| Page/section headings, cards, form sections, notices, disclosures, empty rows, hardware controls and confirmation | [parts.html](weightroom/pages/parts.html) |
| KPI bands, labels, values and notes across pages, fragments and previews | [kpi_parts.html](weightroom/pages/kpi_parts.html): `kpi_row`, `kpi`, `kpi_value`; Telemetry-style surface, spacing and responsive type in [style.css](weightroom/static/style.css). Preserve domain formatters and update hooks; keep progress bars and charts in the KPI caller block instead of duplicating KPI markup or styling. |
| Documentation sections, contents, tables, notes and code blocks | [docs_parts.html](weightroom/pages/docs_parts.html) |
| Pill links, view buttons, radio choices and pagination | [navigation_parts.html](weightroom/pages/navigation_parts.html), [pagination.html](weightroom/pages/pagination.html) |
| User/group filters | [filter_parts.html](weightroom/pages/filter_parts.html) |
| Usage amounts and totals; the Usage dashboard's filter rail, token and cost bands, rates, chart card, tables and notes | [usage_parts.html](weightroom/pages/usage_parts.html), arranged by [usage_content.html](weightroom/pages/usage_content.html); [usage.css](weightroom/static/usage.css); [usage.js](weightroom/static/usage.js) (draws the chart, trend lines and cost bars, redraws on theme change and announces each redraw as `weightroom:usage`) |
| Standalone Usage dashboard proposals (sample data; the live page combines 01 Briefing with 02 Analyst's filter rail) | [usage_designs.html](weightroom/pages/usage_designs.html), rendered with `scripts/preview-usage-designs.py`; each proposal arranges the `usage_parts.html` macros and is checked against the live page's fields in [test_usage_designs.py](tests/test_usage_designs.py) |
| Settings forms and rows | [settings_program.html](weightroom/pages/settings_program.html), [setting_row.html](weightroom/pages/setting_row.html) |
| Chat options, model choices and prompt navigation | [chat_main.html](weightroom/pages/chat_main.html), [chat_navigation.html](weightroom/pages/chat_navigation.html) |
| Deferred loading and retry | [deferred.html](weightroom/pages/deferred.html), [deferred_error.html](weightroom/pages/deferred_error.html) |
| Model fields and engine-specific controls | [model_form.html](weightroom/pages/model_form.html), [engines/](weightroom/pages/engines/) |
| A model's settings by node (node matrix: node columns, groups, cells, shared rows) | [model_matrix_parts.html](weightroom/pages/model_matrix_parts.html), each engine's `engines/<id>/model_matrix.html`, [model-matrix.js](weightroom/static/model-matrix.js) |
| File lists and locations | [files_parts.html](weightroom/pages/files_parts.html) |
| Standalone Files design previews (sample data) | [files_designs.html](weightroom/pages/files_designs.html), rendered with `scripts/preview-files-designs.py` |
| Standalone Status/Telemetry design previews (sample data) | [operations_designs.html](weightroom/pages/operations_designs.html), rendered with `scripts/preview-operations-designs.py` |
| Status request and capacity design previews (sample data) | [status_designs.html](weightroom/pages/status_designs.html), rendered with `scripts/preview-operations-designs.py --status` |
| Selected Telemetry workspace preview (sample data) | [telemetry_design.html](weightroom/pages/telemetry_design.html), rendered with `scripts/preview-operations-designs.py --telemetry` |
| Standalone model settings design previews (sample data) | [model_designs.html](weightroom/pages/model_designs.html), rendered with `scripts/preview-model-designs.py` |
| Live Dry fit split browser, catalogue/selection rows, resource bars and named sets | [fit.html](weightroom/pages/fit.html), shared [fit_parts.html](weightroom/pages/fit_parts.html), [fit_loadouts.html](weightroom/pages/fit_loadouts.html), [fit.css](weightroom/static/fit.css), [fit.js](weightroom/static/fit.js); node-local planning data in [fit.py](weightroom/fit.py) |
| Standalone Dry fit proposals (sample data; option 02 selected for the live page) | [fit_designs.html](weightroom/pages/fit_designs.html), using `fit_parts.html` and `fit.css`, rendered with `scripts/preview-fit-designs.py`; implementation checklist in [model-image-settings-plan.md](docs/model-image-settings-plan.md#7-dry-fit-split-browser-2026-11-11) |
| Standalone Roles design previews (sample data) | [role_designs.html](weightroom/pages/role_designs.html), shared [role_design_parts.html](weightroom/pages/role_design_parts.html), rendered with `scripts/preview-role-designs.py` |
| Live Roles studio, model weights, advanced preferences and routing evidence | [roles.html](weightroom/pages/roles.html), shared [role_parts.html](weightroom/pages/role_parts.html), [role_settings.html](weightroom/pages/role_settings.html), [role_evidence.html](weightroom/pages/role_evidence.html), [roles.css](weightroom/static/roles.css), [role-studio.js](weightroom/static/role-studio.js); drafts stay in page memory across role switches, previews use saved settings |
| Standalone DB design previews (sample data) | [database_designs.html](weightroom/pages/database_designs.html), rendered with `scripts/preview-database-designs.py`; implementation and operating limits in [database-storage.md](docs/database-storage.md#db-operations-desk) |
| DB operations desk, database rows and nested table inventories | [databases.html](weightroom/pages/databases.html), [database_parts.html](weightroom/pages/database_parts.html), [databases.js](weightroom/static/databases.js); the sample proposals also use `database_parts.html` |
| Live Status lanes, capacity ledger and request rows | [status.html](weightroom/pages/status.html), [status_nodes.html](weightroom/pages/status_nodes.html), [status_parts.html](weightroom/pages/status_parts.html), [current_requests.html](weightroom/pages/current_requests.html) |
| Physical-host Telemetry workspace, shared host heading with uptime, Components inventory pills, device tables and graphs | [telemetry_workspace.html](weightroom/pages/telemetry_workspace.html), [telemetry_parts.html](weightroom/pages/telemetry_parts.html), [telemetry-workspace.js](weightroom/static/telemetry-workspace.js); the right column holds model pills, installed services/engines from the Components inventory, device capacity and Wake from [host_wake.html](weightroom/pages/host_wake.html) |
| Node GPU assignments and GPU/host RAM headroom policy | [cards.html](weightroom/pages/cards.html) |
| Node inference runtimes and llama.cpp loading/KV cache defaults | [engine_nodes.html](weightroom/pages/engine_nodes.html); defaults retain their LoadCoach settings save/reset actions |
| Compute node navigator, choices, security and lifecycle | [compute_nodes.html](weightroom/pages/compute_nodes.html), [compute_node_parts.html](weightroom/pages/compute_node_parts.html), [compute_node_access.html](weightroom/pages/compute_node_access.html), [compute-nodes.js](weightroom/static/compute-nodes.js); drafts remain in page memory across node switches and section saves |
| Standalone Compute nodes proposals and shared preview rows (sample data) | [node_designs.html](weightroom/pages/node_designs.html), [node_design_parts.html](weightroom/pages/node_design_parts.html), rendered with `scripts/preview-node-designs.py` |
| Live Users directory and account page (01 Account directory: accounts beside the chosen one; permissions, limits and tools saved together as `do=save`) | [users.html](weightroom/pages/users.html) for `/users` and `/users/<name>`, shared account avatar, status, directory row and ability categories in [user_parts.html](weightroom/pages/user_parts.html), ability ticks in [abilities.html](weightroom/pages/abilities.html), deferred 30 days and spend in [user_activity.html](weightroom/pages/user_activity.html) (only with `see-log`), [users.css](weightroom/static/users.css), [users.js](weightroom/static/users.js) (also drives the Groups directory); drafts remain in page memory across account switches |
| Live Groups directory and group page (laid out as the Users directory: groups beside the chosen one; permissions, limits, models and tools saved together as `do=save`) | [groups.html](weightroom/pages/groups.html) for `/groups` and `/groups/<name>`, directory row `group_choice` in [user_parts.html](weightroom/pages/user_parts.html), deferred 30 days, busiest members and spend in [user_activity.html](weightroom/pages/user_activity.html) (only with `see-log`), [users.css](weightroom/static/users.css), [users.js](weightroom/static/users.js); drafts remain in page memory across group switches |
| Standalone Users proposals (sample data; 01 Account directory is live, 02 Access ledger, 03 Group lanes) | [users_designs.html](weightroom/pages/users_designs.html), shared [user_design_parts.html](weightroom/pages/user_design_parts.html), [user-designs.css](weightroom/static/user-designs.css), [user-designs.js](weightroom/static/user-designs.js), rendered with `scripts/preview-user-designs.py`; reuses the live `user_parts.html` and `users.css` |
| Node/component summaries and model details | [machines.html](weightroom/pages/machines.html), [components_content.html](weightroom/pages/components_content.html), [multimedia_model.html](weightroom/pages/multimedia_model.html) |
| Benchmark Runs studio, bounded model picker, node overview, target choices, run controls and evidence | [benchmark_studio.html](weightroom/pages/benchmark_studio.html), [benchmark_nodes.html](weightroom/pages/benchmark_nodes.html), [benchmark_parts.html](weightroom/pages/benchmark_parts.html) and adjacent `benchmark_*.html` includes; [benchmark-runs.css](weightroom/static/benchmark-runs.css), [benchmarks.js](weightroom/static/benchmarks.js); one row per model with node choices, search and per-model/per-node selection in a scrollable list |
| Standalone benchmark Runs proposals and shared preview components (sample data) | [benchmark_designs.html](weightroom/pages/benchmark_designs.html), [benchmark_design_parts.html](weightroom/pages/benchmark_design_parts.html), rendered with `scripts/preview-benchmark-designs.py`; review findings in [model-image-settings-plan.md](docs/model-image-settings-plan.md#8-benchmark-runs-redesign-proposals-2026-11-11) |
| Live Node measurements fleet matrix, node summaries and evidence cells | [node_measurements.html](weightroom/pages/node_measurements.html), shared [node_measurement_parts.html](weightroom/pages/node_measurement_parts.html), model-type navigation in [benchmark_parts.html](weightroom/pages/benchmark_parts.html), [node-measurements.css](weightroom/static/node-measurements.css), [node-measurements.js](weightroom/static/node-measurements.js); scoped data in [benchmark_views.py](weightroom/benchmark_views.py); compute nodes only, model-type pills below nodes, bounded rows, column sorting and value/score display |
| Standalone Node measurements proposals (sample data; 02 Fleet matrix is live) | [node_measurement_designs.html](weightroom/pages/node_measurement_designs.html), preview DOM templates in [node_measurement_design_parts.html](weightroom/pages/node_measurement_design_parts.html), shared `node_measurement_parts.html` and `node-measurements.css`, [node-measurement-designs.css](weightroom/static/node-measurement-designs.css), [node-measurement-designs.js](weightroom/static/node-measurement-designs.js), rendered with `scripts/preview-node-measurement-designs.py`; 15–30 LLM models per populated sample node |
| Icons | [icons.html](weightroom/pages/icons.html) |
| Image files, discovery cards, workflows and editor warnings | [mirrorwall_files.html](mirrorwall/pages/mirrorwall_files.html), [mirrorwall_discovery.html](mirrorwall/pages/mirrorwall_discovery.html), [mirrorwall_workflows.html](mirrorwall/pages/mirrorwall_workflows.html), [mirrorwall_models.html](mirrorwall/pages/mirrorwall_models.html) |
| Audio player and preferences | [soundroom_player.html](soundroom/pages/soundroom_player.html), [soundroom_preferences.html](soundroom/pages/soundroom_preferences.html) |
| Standalone API/MCP diagnostic layout | [diagnostic_frame.html](loadcoach/pages/diagnostic_frame.html), used by [LoadCoach](loadcoach/test.html) and [ToolLocker](toollocker/test.html) |
| Standalone voice client | [index.html](voice/static/index.html) and [voice.css](voice/static/voice.css) (independent static client) |
| Shared appearance and browser behavior | [style.css](weightroom/static/style.css), [console.js](weightroom/static/console.js), [model-settings.js](weightroom/static/model-settings.js) |

For UI changes, run the component audit in [test_ui_consistency.py](tests/test_ui_consistency.py)
and the affected feature tests. Shared component changes also require the all-page browser
checks there, including narrow layouts, empty/error states and dynamic replacements.

A form whose unsent changes must survive a polling refresh of its fragment carries
`data-hold-refresh`; [pieces.js](weightroom/static/pieces.js) then defers the swap, as it does
for selected text.

Keep this index current when introducing or moving shared components. Record UI conventions
here; Claude's entry point imports this file.
