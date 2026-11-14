<!--docs
title = "Documentation tools"
summary = "Search and read this library through caller-bound MCP tools."
order = 95
covers = []
-->
# Documentation tools

Give an MCP client the same documentation visibility as its actual caller.

## Before you start

The account needs [tool access](/docs/system/abilities#ability-use-tools) and permitted `docs__*` tool patterns. The built-in server is available automatically when ToolLocker has a console URL and no operator-defined `docs` server replaces it. Source and administrator references still require [settings-management access](/docs/system/abilities#ability-manage-settings).

## Do it

1. Open **Settings → Tools → Try a tool** and choose **docs** under **Tool set**, or connect a client to `/mcp/docs` using its own account key.
2. Begin with `docs__list_topics` or `docs__search`, then use `docs__read` with a returned URL. Follow `next_offset` to continue a long article.
3. Use `docs__find_endpoint`, `docs__lookup_setting` or `docs__explain_ability` for generated administrator references. `docs__code_outline` and `docs__code_symbol` read documented allowlisted modules.

## Limits and troubleshooting

The adapter forwards signed caller proof; the console applies visibility to the shared library. Results carry citation URLs. Tool patterns, tool sets and sensitivity still narrow access. A refused admin lookup tells a user to ask an administrator. Source input must name an allowlisted module, never a key or arbitrary path. Settings results contain shipped defaults, never live values.

If the gateway is unavailable, use normal [Documentation search](/docs). Operator-defined servers retain their normal configuration; the built-in adapter does not replace them.

## Related

[Declared API endpoints](/docs/api/custom) · [ToolLocker setup](/docs/modules/toollocker) · [Tools](/docs/web-app/tools)
