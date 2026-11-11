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
| Documentation sections, contents, tables, notes and code blocks | [docs_parts.html](weightroom/pages/docs_parts.html) |
| Pill links, view buttons, radio choices and pagination | [navigation_parts.html](weightroom/pages/navigation_parts.html), [pagination.html](weightroom/pages/pagination.html) |
| User/group filters | [filter_parts.html](weightroom/pages/filter_parts.html) |
| Usage amounts and totals | [usage_parts.html](weightroom/pages/usage_parts.html), [usage_kpis.html](weightroom/pages/usage_kpis.html) |
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
| Standalone DB design previews (sample data) | [database_designs.html](weightroom/pages/database_designs.html), rendered with `scripts/preview-database-designs.py`; implementation and operating limits in [database-storage.md](docs/database-storage.md#db-operations-desk) |
| DB operations desk, database rows and nested table inventories | [databases.html](weightroom/pages/databases.html), [database_parts.html](weightroom/pages/database_parts.html), [databases.js](weightroom/static/databases.js); the sample proposals also use `database_parts.html` |
| Live Status lanes, capacity ledger and request rows | [status.html](weightroom/pages/status.html), [status_nodes.html](weightroom/pages/status_nodes.html), [status_parts.html](weightroom/pages/status_parts.html), [current_requests.html](weightroom/pages/current_requests.html) |
| Physical-host Telemetry workspace, device tables and graphs | [telemetry_workspace.html](weightroom/pages/telemetry_workspace.html), [telemetry_parts.html](weightroom/pages/telemetry_parts.html), [telemetry-workspace.js](weightroom/static/telemetry-workspace.js) |
| Node GPU assignments and RAM policy | [cards.html](weightroom/pages/cards.html) |
| Compute node navigator, choices, security and lifecycle | [compute_nodes.html](weightroom/pages/compute_nodes.html), [compute_node_parts.html](weightroom/pages/compute_node_parts.html), [compute_node_access.html](weightroom/pages/compute_node_access.html), [compute-nodes.js](weightroom/static/compute-nodes.js); drafts remain in page memory across node switches and section saves |
| Standalone Compute nodes proposals and shared preview rows (sample data) | [node_designs.html](weightroom/pages/node_designs.html), [node_design_parts.html](weightroom/pages/node_design_parts.html), rendered with `scripts/preview-node-designs.py` |
| Node/component summaries and model details | [machines.html](weightroom/pages/machines.html), [components_content.html](weightroom/pages/components_content.html), [multimedia_model.html](weightroom/pages/multimedia_model.html) |
| Benchmark values and shared panels | [benchmark_parts.html](weightroom/pages/benchmark_parts.html) and adjacent `benchmark_*.html` includes |
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
