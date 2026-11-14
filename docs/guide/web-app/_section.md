<!--docs
title = "Web app"
summary = "Using the web console: chat, collections, workflows, voice and media, tools, and your API keys."
order = 30
audience = "public"
covers = ["/docs/web-app"]
-->
# Web app

## Finding your way {#console}

The bar along the top holds the console's categories, and the pages of the one you are in are listed down the left. On a phone, open Menu for all of them. What you see depends on your account: a category appears only when it has a page you may use.

| Category | What is there |
| --- | --- |
| Interact | Text chat; [Tool chat](/chat/tools) when use-tools and visible ToolLocker tools allow it; image generation, music, sounds, Voice generation and Transcribe; and Voice chat when your account may use it. |
| Models | Models, image and audio models, roles, model files and downloads, for accounts that manage models. |
| Benchmark | Benchmark runs, results, node measurements (including embedders) and comparisons, and the collections and workflows used to compare models' grounded answers. |
| Logs | Usage, reliability, model requests, batches, event log, plugin logs and system logs. |
| System | Status and telemetry; administrators also see components, alerts, schedules, databases and backups. |
| Settings | Compute nodes, users, groups, tools, plugins and each program's settings, for administrators. |
| Docs | These pages. |

Your name, at the top right, opens [My account](/account): your password, sign-in security and API keys, the console's appearance, audio, and your own logs. A page with a Help button opens its topic here.

## Tool chat {#tool-chat}

Open Interact → Chat & analysis → [Tool chat](/chat/tools). The link appears when your account has `use-tools` and at least one visible ToolLocker tool. Tools remain limited by your group, account and current sensitivity clearance.

Search the tool names and descriptions, choose a narrowing tool set, then select a tool. Its fields show types, required values and enums. Use Raw JSON for nested arguments or additional properties. Run tool calls it immediately with your own permissions. Each result shows all content parts, `isError` and an Exact result JSON disclosure.

To ask a model, choose a permitted tool-capable model and send a message. Every proposed call waits for Run, Edit arguments followed by Run, or Decline with an optional reason. After all proposals have results, the model continues. Only a bounded copy of each result reaches the model; the exact output stays in history.

Tool chat keeps separate private conversations. Double-click a title or press F2 to rename it; deletion asks twice. Stop cancels the current chat task. Interrupted calls are recorded as errors and never replayed automatically. Lowered clearance hides earlier conversations above it. Tool results can raise a conversation’s level; a sensitive result stops cloud continuation before it is sent.

The same administrator-configured call/time limits as [Responses tools](/docs/api/openai) apply, including approval waits. Settings → Tools → Try a tool remains an administrator connection checker.
