<!--docs
title = "API keys"
summary = "Create private application keys and connect a model client."
order = 80
covers = ["/account/keys"]
-->
# API keys

Give an application a separate key that acts with your account's current permissions.

## Create a key {#api}

1. Open **My account → API keys**.
2. Enter **Label for a new key**, choose **Expires after**, and select **Make a key**.
3. Copy it when **Copy this key now** appears. The key is shown once; store it privately in your client.

## Connect a client {#api-connect}

Use an OpenAI-compatible client with model API base `http://CONTROL_NODE:8442/v1`, or the address supplied by your administrator. Use that supplied address when the installation has a different host, port or HTTPS gateway. Send your key as a Bearer token and select a model or role offered to your account.

To replace a key, create its replacement, update the client and check access before choosing **Remove…** on the old key. Removal stops that key's access immediately. Key expiry choices follow site policy.

<!-- audience: admin -->
[API reference](/docs/api) contains the generated endpoints and compatibility guidance.
<!-- /audience -->

## Limits and troubleshooting

An expired or removed key cannot be recovered. Make another key if you did not save it. Never paste a key into a conversation, screenshot or issue report. Browser sign-ins are managed separately under **My account → Account**.
