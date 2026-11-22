<!--docs
title = "Tools"
summary = "Connect an MCP client or review calls in Tool chat."
order = 70
covers = ["/chat/tools"]
-->
# Tools

Use the tools your account is offered through an MCP client or **Tool chat**.

## Before you start

Ask an administrator for [tool access](/docs/system/abilities#ability-use-tools) and the appropriate tool patterns. The chosen tool set and sensitivity level can narrow what appears.

## Connect a client {#tools}

Use MCP address `http://CONTROL_NODE:8443/mcp` and a private [application key](/docs/web-app/api-keys). A named tool set uses `/mcp/TOOLSET`. Your group and account permissions still apply to every call.

The built-in extraction tools work with your private uploaded image assets. In [Image extraction](/docs/web-app/image-extraction), open **Use in API or tools** to copy an asset ID. Tool results retain authenticated status and export references; they do not grant another account access to your images.

## Use Tool chat

1. Open **Tool chat** and choose **Tool set**. Use **Search tools** to inspect the available descriptions.
2. For a direct call, choose **Tool**, fill its arguments and select **Run tool**. For nested or advanced inputs, enable **Use raw JSON**; typed fields are inactive until you switch back.
3. To ask a model, send a message in **Ask with tools**. Review each proposed call and choose **Run**, **Edit arguments** or **Decline**. Inspect the exact tool result before using it.

Tool chat keeps separate private conversations. It can call a tool directly even when no tool-capable model is available. **Stop** ends the model's answer on its compute node as well, and an interrupted tool call is never replayed.

## When no tools appear {#tools-missing}

Ask an administrator to check the service, server configuration and your tool access.

<!-- audience: admin -->
Open [Settings → Tools](/tools) to connect and try a tool. See [ToolLocker setup and adding tools](/docs/modules/toollocker) for the gateway requirements and Python example.
<!-- /audience -->

## Related

[Chat](/docs/web-app/chat) · [Image extraction](/docs/web-app/image-extraction)
