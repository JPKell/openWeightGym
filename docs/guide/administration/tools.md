<!--docs
title = "Tools administration"
summary = "Inspect tool servers, access levels, sets and real connection checks."
order = 60
covers = ["/tools"]
-->
# Tools administration

Inspect tool servers, access levels, sets and real connection checks.

## Do it

1. Open **Settings → Tools** and review **Tool service**, **Servers** and their reported configuration time.
2. Use **Tool levels** and **Tool sets** to narrow access. Save the intended section.
3. Under **Try a tool**, select a set/tool, inspect its schema, supply arguments and run it. Results use the signed-in caller.

## Options

A superuser can manage remote MCP servers through the shared remote-server editor. Machine-file servers remain authoritative for their names. [ToolLocker setup](/docs/modules/toollocker) is the installation and authoring home; [Documentation tools](/docs/api/docs-tools) covers `docs__*`.

## Limits and troubleshooting

A tool set only narrows what an account can already see. Sensitivity, tool patterns and model tool permission remain separate checks. Arguments and results can contain private data; inspect the target tool before calling it. An unavailable server is distinct from an empty permitted catalogue.

[Shared form controls and confirmations](/docs/getting-started/navigation#forms-and-confirmations)
