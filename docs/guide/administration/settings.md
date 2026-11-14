<!--docs
title = "General settings"
summary = "Find, change, reset and transfer settings across the right owning nodes."
order = 10
covers = ["/settings", "/settings/requests", "/settings/retention", "/settings/alerts", "/settings/limits", "/settings/downloads", "/settings/machine", "/settings/history", "/settings/security"]
-->
# General settings

Find, change, reset and transfer settings across the right owning nodes.

## Before you start

Use [settings-management access](/docs/system/abilities#ability-manage-settings). Meanings and shipped defaults live in the [Settings reference](/docs/operations/settings-reference); current policy behavior is in [Settings policies and restarts](/docs/operations/settings-policy).

## Do it

1. Choose the settings page for the task, then use search or **Changed only** to find a field.
2. Review its owning program/node and change status, edit it and save. Reset an override to resume the file/default value.
3. Review **Settings history** for a single-change revert, or use **Transfer settings → Preview import** and **Confirm import** for a validated transfer.

## Options

| Page | Use |
|---|---|
| **General settings** | Platform service switches, presentation and site-wide defaults. |
| **Requests and queue** | Request admission and routing policies. |
| **Retention and privacy** | Retained data and built-in backup policy. |
| **Alerts and notifications** | Operational thresholds, delivery configuration and status. |
| **Security and sign-in** | Password, key, session and sign-in failure policy. |
| **Uploads and limits** | Content bounds for new work. |
| **Downloads and credentials** | Node-owned provider credentials and transfer admission. |
| **Machine configuration** | Read-only connection/source evidence and compatibility information. |
| **Settings history** | Recorded changes, guarded revert and secret-free transfer. |

## Limits and troubleshooting

**Waiting for restart** records a pending setting; saving alone does not make it effective. Use the existing node-scoped restart control only after reviewing active work and its confirmation. Blank credential submissions retain saved values; use the explicit remove action to delete one. Saved credentials stay hidden. File-only settings remain read-only in the console.
