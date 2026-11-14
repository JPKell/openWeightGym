<!--docs
title = "Errors and limits"
summary = "Find the refusal reason and choose a safe retry or configuration fix."
order = 40
covers = []
-->
# Errors and limits

Use the response status, error body and retained request to understand a failed call.

## Do it

1. Keep the HTTP status and error `message`/`code` when present. Record request identifiers from [response headers](/docs/api/headers#api-response-headers).
2. Open [Model requests](/docs/monitoring/requests) for admission and execution details, or [Event log](/docs/monitoring/event-log) for the exact trace.
3. Correct input, access or configuration before retrying. Follow `Retry-After` when returned for a transient refusal.

## Options

| Outcome | First check |
| --- | --- |
| 400 | Unsupported parameter, invalid JSON/schema, incompatible input or an invalid header combination. |
| 401 | Key is missing, invalid, revoked or expired. |
| 403 | The account, its group or its application session lacks access. `tools_not_allowed` means the request needs `use-tools`. |
| 404 | Resource is missing or outside the caller's visibility. Hidden stored Responses deliberately use the same result as missing records. |
| 409 | A guarded identity changed or an operation conflicts with current state. Read the error before repeating it. |
| 429 / 503 | Inspect allowance, capacity, dependency and model availability evidence; a retry cannot fix exhausted spending or a disabled component. |
| Streaming failure | Read the terminal event; an HTTP connection that started successfully can still end with incomplete output. |

These are investigation cues, not one shared error schema for every backend. Some provider failures pass through their own envelope.

## Limits and troubleshooting

Find configured admission, retention, spending and tool-loop defaults in the [settings reference](/docs/operations/settings-reference), and saved policy in [Settings](/docs/administration/settings) or [Allowances](/docs/administration/allowances). The reference never shows live values.

Avoid blindly retrying writes, paid provider calls or batch submission: an interrupted client does not prove the server did no work. Check the retained record first. Tool-loop limit exhaustion produces an incomplete Responses result; its reason identifies call or time exhaustion.

## Related

[Reliability](/docs/monitoring/reliability), [Components](/docs/administration/components).
