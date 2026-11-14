<!--docs
title = "API keys"
summary = "Create a key and connect an OpenAI-compatible client to the model API."
order = 80
covers = ["/account/keys"]
-->
# API keys

## Create a key {#api}

Open [My account → API keys](/account/keys) to create a key. Copy it when shown; it cannot be displayed again. Give it to your client as its API key, and keep it private. Removing a key ends access for clients using that key.

## Connect a client {#api-connect}

The standard model API address on this control node is `http://CONTROL_NODE:8442/v1`, for trusted LAN or VPN access. If your administrator configured a different host, port or HTTPS gateway, use that address instead.

Use an OpenAI-compatible client, choose an allowed model, and send the key as a Bearer token. The model list is `GET /v1/models`. Chat uses `POST /v1/chat/completions`. Your usual model permissions and usage limits apply to API requests.

<!-- audience: admin -->
Every endpoint and header is in the [API reference](/docs/api).
<!-- /audience -->
