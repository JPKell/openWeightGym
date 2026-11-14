<!--docs
title = "Troubleshooting"
summary = "Narrow failures using request evidence, permissions, models and services."
order = 80
covers = []
-->
# Troubleshooting

Start from the failed operation's evidence before changing configuration.

## Do it

1. Open [Model requests](/docs/monitoring/requests). Keep its request ID and inspect admission, selected model/node and errors.
2. Follow its [Event log](/docs/monitoring/event-log) trace and [System logs](/docs/monitoring/system-logs) context. A historical failure can remain after the current issue clears.
3. Check [Status](/docs/monitoring/status) for queue/residency, [Telemetry](/docs/monitoring/telemetry) for resources and [Alerts](/docs/monitoring/alerts) for episodes. Missing observations are not recovery evidence.

## Options

| Symptom | Check next |
| --- | --- |
| Page, model or tool absent | Account/group access, application-session restrictions and [Components](/docs/administration/components). |
| Model will not answer | Node availability, wake configuration, installed runtime, files and [Reliability](/docs/monitoring/reliability). |
| Request waits | Admission evidence, active work, holds/locks and memory. [Dry fit](/docs/model-guides/dry-fit) loads nothing. |
| Settings save rejected | Validation and owning-node availability. Keep the draft and read the message. |
| Setting appears unchanged | Inheritance, node scope and restart notices in [Settings](/docs/administration/settings). |
| Tools fail | The caller-scoped test in [Tools](/docs/administration/tools), then [ToolLocker setup](/docs/modules/toollocker). |
| Voice/media fails | Its [Web app guide](/docs/web-app) and runtime/model prerequisites. |

## Recovery

Preserve keys, settings, databases and model files. [Installation](/docs/operations/installation) owns update, repair and uninstall instructions. Authorized updates use `owg update; owg restart`; verify the effective running source and live behavior afterward. Restarts can interrupt requests and calls; inspect scope first.

## Related

[Errors and limits](/docs/api/errors), [Backups](/docs/data/backups), [Where things run](/docs/operations/topology).
