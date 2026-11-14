<!--docs
title = "Authentication and keys"
summary = "Connect an API client with its own account key and permitted model or role."
order = 5
covers = []
-->
# Authentication and keys

Connect clients to the control node using an account key.

## Before you start

Create a key in [My account → API keys](/docs/web-app/api-keys). The client inherits that account's model access, abilities, spending rules and sensitivity ceiling. An application session can narrow access further.

## Do it

1. Set the OpenAI-compatible base URL to the model listener's `/v1` address. Find the configured address in [Where things run](/docs/operations/topology).
2. Put the key in the client's API-key field or send `Authorization: Bearer API_KEY`. Keep it outside source files and shared request captures.
3. List available models before choosing a configured model or role. A role can select a different permitted model for each request; see [Roles and routing](/docs/model-guides/roles).

Use the tool listener for MCP clients and the console address for Documentation JSON reads. These are separate listeners, not paths beneath the model API's base URL.

## Limits and troubleshooting

`Missing or wrong API key.` means the request has no currently valid key. Check expiry, revocation and application-session restrictions before generating another key. Console cookies are for the Web app; external clients should use their own key.

## Related

[Request examples](/docs/api/examples), [Headers](/docs/api/headers), [Errors and limits](/docs/api/errors).
