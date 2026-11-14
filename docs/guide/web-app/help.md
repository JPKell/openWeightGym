<!--docs
title = "Search and Ask"
summary = "Find guides or ask a cited question using the documentation tools."
order = 130
covers = []
-->
# Search and Ask

Use **Docs** to search visible guides or ask for an answer with source links.

## Do it

1. Open **Docs → Documentation**. Search words from a task, control or message, and optionally choose a section. Search works without a model or JavaScript.
2. Select **Ask**, enter a **Question** and choose **Ask**. Open the source links to check the evidence behind the answer.
3. Continue in the same tab or select **Clear conversation** to start again. A draft survives a refresh in that tab; closing the tab ends its browser history.

## Before you start

Ask needs [tool access](/docs/web-app/tools) and a configured help role. Your model permissions, tool patterns, sensitivity policy, spending rules and normal usage logging apply. If **Ask** is absent, use **Search** or ask an administrator about tool access.

## Privacy and limits

Ask does not create a stored conversation or stored Responses chain. Its messages live in the browser tab; normal model requests and tool calls still follow their existing logging/retention policy. Unanswered question text is kept anonymously for documentation improvements; see [My account privacy](/docs/web-app/my-account#privacy).

Answers use the same visible library as Search. Administrative topics remain restricted. Ask can explain a documented task but cannot change settings or execute an operating action. Treat its citations as the place to verify a claim.

If the model returns nothing, fails, reaches allowance/capacity policy or cannot reach ToolLocker, the panel offers **Search documentation**. A configured role can still have no eligible permitted model; an administrator must check model access and readiness.

<!-- audience: admin -->
## Configure the help role

Open **Models → Roles**, expand **Create a role**, enter `docs-help` under **New role** and choose a chat model that supports tools. Save its candidates and weights using [Roles and routing](/docs/model-guides/roles). The application supplies the documentation prompt and caller-scoped sources; there is no separate knowledge base. For different group access, use the existing [group model policy](/docs/administration/groups) and role candidates. Ask uses the shared role rather than a separate per-group model picker.

Ask offers documentation tools and any `code__*` tools already offered to an administrator by ToolLocker. It never adds permissions. Test source tools in [Settings → Tools](/docs/administration/tools) if listing or calls are refused.

## Review documentation gaps

Open **Docs → Gaps** to review questions the agent marked as undocumented. The control node retains a bounded, deduplicated list with count and last-asked time, without account or answer fields. **Dismiss** removes one entry. Update the canonical guide or source description when a gap is useful; do not add a separate FAQ.
<!-- /audience -->

## Related

[Find your way](/docs/getting-started/navigation), [Tools](/docs/web-app/tools).
