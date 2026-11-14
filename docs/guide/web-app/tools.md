<!--docs
title = "Tools"
summary = "Give an MCP client or agent the tools your account may use."
order = 70
covers = ["/chat/tools"]
-->
# Tools

## Connect a client {#tools}

ToolLocker offers permitted tools to an MCP-capable client or agent at `http://CONTROL_NODE:8443/mcp`, using a key from My account. Your group, account, sensitivity level and chosen tool set determine which tools are available.

The built-in `extraction__` tools describe images, extract text, receipts, tables or custom fields, and inspect or cancel jobs. Upload the source in [Image extraction](/chat/extraction) and copy its asset ID from “Use in API or tools”. Tools use that source under your own account, model permissions and usage limits; results include authenticated status and export references.

## When no tools appear {#tools-missing}

Ask an administrator to check the tool service, configured servers and your tool access.

<!-- audience: admin -->
Open [Settings → Tools](/tools) to connect and try a tool. See [ToolLocker setup and adding tools](/docs/modules/toollocker) for the gateway requirements and Python example.
<!-- /audience -->
