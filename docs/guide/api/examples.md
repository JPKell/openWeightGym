<!--docs
title = "API examples"
summary = "Try small model and documentation requests before integrating a client."
order = 60
covers = []
-->
# API examples

Run small requests with the caller's own key before sending larger work.

## Before you start

Set `OWG_API` to the configured model listener, `OWG_CONSOLE` to the console address, `OWG_KEY` to an account key and `OWG_MODEL` to an accessible chat model or role. Addresses below omit the trailing slash. See [Authentication](/docs/api/authentication); avoid printing the key in captured logs.

## Do it

### Chat

```sh
curl --fail-with-body "$OWG_API/v1/chat/completions" \
  -H "Authorization: Bearer $OWG_KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$OWG_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello.\"}]}"
```

Read the answer from `choices[0].message.content`. Its text depends on the model.

### Responses without retained conversation state

```sh
curl --fail-with-body "$OWG_API/v1/responses" \
  -H "Authorization: Bearer $OWG_KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$OWG_MODEL\",\"input\":\"Say hello.\",\"store\":false}"
```

A completed result has `object: response`, `status: completed` and an assistant `message` in `output`; its `content` contains `output_text`. Normal request usage and logging still apply with `store:false`.

### Search the same documentation as the Web app

```sh
curl --fail-with-body --get "$OWG_CONSOLE/owg/docs/search" \
  -H "Authorization: Bearer $OWG_KEY" --data-urlencode 'q=Shift+Enter' --data-urlencode 'limit=3'
```

This requires `use-tools`. The JSON `hits` contain titles, headings, snippets and citation URLs. The account sees only its visible guides. Open a URL in the console or pass it to `docs__read`; administrative references remain protected.

## Limits and troubleshooting

The examples verify protocol shapes against a local isolated instance with a fake model. They do not promise a particular model answer. For streaming, add `stream:true` and consume the matching chat or Responses event protocol.

## Related

[MCP endpoint](/docs/api/mcp), [Errors and limits](/docs/api/errors), [Headers](/docs/api/headers).
