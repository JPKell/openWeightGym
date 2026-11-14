<!--docs
title = "Where things run"
summary = "Find the control node, compute nodes and configured client connections."
order = 20
covers = []
-->
# Where things run

The control node owns shared application services; compute nodes run inference.

## Components

| Component | Responsibility |
| --- | --- |
| WeightRoom | Console, conversations, collections/workflows and benchmark coordination. |
| Turnstile | Authentication, model/role routing, hosted calls and signed compute requests. |
| ToolLocker | MCP gateway; tools execute on their owning hosts. |
| Voice gateway | Browser, desktop and telephone connections and adapter integration. |
| LoadCoach | Compute-node model processes, admission, inference and scheduling. |
| Spotter | Host telemetry and permitted service/power controls. |
| Voice service | Compute-side speech recognition and synthesis. |

Services run side by side on a single machine. Existing machine IDs, keys and installer options retain `front` where compatibility requires it.

## Connect

Use addresses printed by the installer or `owg status`. [Installation](/docs/operations/installation) owns listener defaults and setup. API clients use the model listener with `/v1`; MCP clients use the tool listener with `/mcp` or `/mcp/TOOLSET`. Browser clients use the console or Voice gateway. Private native model ports are not client endpoints.

Create keys through [API keys](/docs/web-app/api-keys). Browser microphone use needs HTTPS or localhost; telephone webhooks have separate signed ingress. Follow Installation's trusted-LAN/VPN and HTTPS instructions.

## Unavailable compute nodes

Accounts and retained records remain on the control node. Remembered permitted models can stay selectable while their node is unavailable; requesting one can ask Turnstile to wake it. Wake needs configured, reachable Wake-on-LAN and host support. [Telemetry](/docs/monitoring/telemetry) explains manual controls; [Compute nodes](/docs/administration/compute-nodes) owns configuration.

Wake waiting is bounded by policy; its default lives in the [settings reference](/docs/operations/settings-reference). Readable retained evidence does not prove node health. Live management can remain unavailable until the node answers.

## Related

[Stores and ownership](/docs/data/stores), [Components](/docs/administration/components), [Troubleshooting](/docs/operations/troubleshooting).
