<!--docs
title = "MCP endpoint"
summary = "Connect an external tool client or run caller-scoped tools through Responses."
order = 50
covers = []
-->
# MCP endpoint

ToolLocker offers permitted tools through the control node's tool listener.

## Before you start

The caller needs [use-tools](/docs/system/abilities#ability-use-tools), matching tool patterns and an allowed sensitivity level. Administrators configure servers and sets in [Tools](/docs/administration/tools). [Built-in Documentation tools](/docs/modules/toollocker#built-in-documentation-tools) explains automatic connection settings and operator overrides.

## Do it

1. Give an MCP client its own account key and the configured tool-listener URL ending in `/mcp/docs` for Documentation tools, or `/mcp` for all offered tools.
2. Let the client initialize an MCP session and list tools. A tool set narrows access; it never grants tools outside the account's rules.
3. Search before reading. Copy each result's canonical `/docs/…#anchor` URL when citing it.

For example, a Claude client can be configured with:

```sh
claude mcp add --transport http owg http://CONTROL:8443/mcp/docs --header "Authorization: Bearer KEY"
```

Replace the address and key with your configured tool listener and caller key. The external client runs its own conversation; this command does not configure the Web app's help model.

## Server-run Responses tools

A Responses request may declare `type: mcp`, `server_label: toollocker` and `server_url: /mcp/docs`. `allowed_tools` narrows the listed tools further. Foreign MCP servers are refused by this server-run adapter.

The existing loop runs under the same caller and trace, rechecks tool access on each call, and uses normal model admission, allowances and logging. Approval policies can pause a stored Responses chain; see [Code references](/docs/code/turnstile?module=turnstile/response_tools.py) for its input contract. Tool chat exposes the same workflow interactively.

The `docs-help` role also runs this same documentation assistant through Responses or chat completions. Send a text question as `input` or user `messages`; it supplies its own prompt and source tools and uses no stored conversation. Follow [Search and Ask](/docs/web-app/help) for setup and privacy.

## Options

| Docs tool | Use |
| --- | --- |
| `docs__list_topics`, `docs__search`, `docs__read` | Discover, find and read visible guides. |
| `docs__find_endpoint`, `docs__lookup_setting`, `docs__explain_ability` | Read generated references; administrators only. |
| `docs__code_outline`, `docs__code_symbol` | Inspect allowlisted source documentation; administrators only. |

Reads are paged and search is bounded. A result's `next_offset` continues a long read or catalogue listing. Source input must name an offered module; file paths cannot escape the source allowlist. Settings tools show defaults and meanings, never live settings or credentials.

## Related

[Tools for everyday use](/docs/web-app/tools), [API examples](/docs/api/examples), [ToolLocker module](/docs/modules/toollocker).
