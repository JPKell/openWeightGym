# Tool use: implementation brief

Written **2026-11-14** for the agent implementing it, after an interview with the owner. It covers
all tool work in [openai-api-plan.md](openai-api-plan.md#milestones) (TA–TD). It **depends on**
milestones R1 (Responses streaming) and R3 (stored Responses) in
[openai-api-work.md](openai-api-work.md), so do that brief's R1 and R3 first.

The "Before you start" and "Shared rules" sections of [openai-api-work.md](openai-api-work.md)
apply here unchanged: no deployment without the owner's request, one commit per milestone
pushed to `main`, stage only your own changes, compare test failures against HEAD, stop and
report when an assumption proves false.

**Not in scope:**
- Per-API-key permissions. Permissions stay per account; a key that needs different rules gets
  its own program account.
- Any MCP server other than this the control node's own ToolLocker.
- Tools in the existing Text chat page.
- `/v1/moderations`.

## How tools work today (read this first)

- **Client-defined tools.** `/v1/chat/completions` already accepts `tools` and passes them to the
  model. The model can only *ask* for a call; the client runs it on its own machine. No
  permission restricts this today.
- **ToolLocker tools** are your server-side tools (files, code, …). An MCP client connects to
  `CONTROL_NODE:8443/mcp` with an API key.
  - `tools/list` returns only what that account may see: the group's `tools` patterns, the
    account's `add_tools`/`remove_tools`, the sensitivity level and the tool sets
    ([toollocker/door.py](../toollocker/door.py), `may_see_tool` in
    [loadcoach/groups.py](../loadcoach/groups.py)).
  - The **client** copies those descriptions into its model request.
  - Each `tools/call` is re-checked and run by ToolLocker. Its level sticks to the request's trace
    (`set_trace_level`), and a raised trace makes the gateway refuse hosted models
    ([turnstile/hosted.py](../turnstile/hosted.py)).
- **The server never adds tools to a model's context.** The console's only tool UI is Settings →
  Tools → **Try a tool** ([tools.html](../weightroom/pages/tools.html),
  [tools.js](../weightroom/static/tools.js), `/tools/message`), and only for `manage-settings`.

## Owner decisions (2026-11-14)

| Question | Decision |
|---|---|
| Restricting client tools | One switch: a new ability **`use-tools`** controls whether an account may send `tools` in **any** model request |
| Models covered | Every model openWeightGym routes: local, roles **and cloud APIs** |
| Levels | Set per group, with the existing per-account `add`/`remove` override. App sessions keep only what both the person and the app have (`limited()`). No per-key permissions |
| Default | Allowed for **every existing group** when it ships |
| Request without permission | **403** with a clear message, never silently dropping the tools |
| ToolLocker access | Unchanged and separate: `tools` patterns still decide which ToolLocker tools an account sees and calls |
| Server-run ToolLocker tools in Responses | **Yes** |
| Approval of server-run calls | The client's `require_approval` decides; the default is **never** |
| Loop limits | **25 tool calls, 15 minutes** per request, both adjustable by an administrator |
| Tool results and cloud models | Follow sensitivity levels. If a result raises the level above what the chosen cloud model may receive, **stop with a clear error**. Never switch models silently |
| New console page | **Tool chat**, under Interact beside Text chat |
| Who may use Tool chat | Anyone with `use-tools` who can see at least one ToolLocker tool |
| Model calls in Tool chat | **The user approves each call**: run, edit the arguments, or decline |
| Tool chat history | **Saved** like Text chat |

## TA. The `use-tools` ability

**Build:**
- Add `"use-tools": "send tool definitions to models and receive tool calls"` to `ABILITIES` in
  [loadcoach/groups.py](../loadcoach/groups.py). `EVERYTHING` and superusers include it
  automatically. Per-account `add`/`remove` (`adjusted()`) and app sessions (`limited()`)
  already work for any ability. Verify them with tests.
- **Default for existing groups:** a migration in the store where groups live adds `use-tools` to
  every existing group's `can`. Also add it to installer and default group templates, and to any
  shipped group presets. Don't add it to program accounts that inherit `can` from a group; they
  get it through the group.
- **Enforcement:** a request body with a non-empty `tools` field, or with `tool_choice`, or with
  messages containing `tool_calls` or `role: tool` items, needs `use-tools`. Without it, return
  403, code `tools_not_allowed`, message
  `"Tool use is not allowed for this account. Ask an administrator for the use-tools ability."`.
  Enforce it in every place a model request enters:
  - LoadCoach `forward` ([loadcoach/app.py](../loadcoach/app.py)), for local models on every node;
  - the hosted path in Turnstile ([turnstile/hosted.py](../turnstile/hosted.py) `answer`);
  - the roles path ([turnstile/roles.py](../turnstile/roles.py));
  - batch lines: a per-line error, not a failed batch;
  - Responses: TB and TC apply the same check before translating.

  Write one helper (e.g. `wants_tools(body)` plus the ability check) and call it from each place;
  don't copy the condition. Hidden or forbidden models keep their 404 behaviour; the tool check
  comes after the model check.
- **UI:** `use-tools` appears in the ability ticks for Users and Groups through the existing
  ability categories ([user_parts.html](../weightroom/pages/user_parts.html),
  [abilities.html](../weightroom/pages/abilities.html)). Put it in the category for model use.
- **Docs:** update [docs/api/openai.html](../weightroom/pages/docs/api/openai.html), the Users/Groups
  help, and [toollocker/README.md](../toollocker/README.md). The README note: removing `use-tools`
  stops tool calling through openWeightGym, but an MCP client talking to a cloud model *directly*
  can still call ToolLocker. To cut off server tools entirely, remove the account's ToolLocker
  tools with `tools` patterns.

**Tests:**
- Each entry point refuses tools without the ability: chat, completions with messages carrying
  tool items, hosted, roles, batch line, Responses.
- Allowed by default after migrating an existing store, on SQLite and PostgreSQL.
- An account `remove` takes it away; an account `add` grants it in a group without it.
- An app session needs both sides.
- Requests without tools are unaffected.

## TB. Function tools in Responses

**Depends on R1.**

**Build** in [turnstile/responses.py](../turnstile/responses.py):
- Accept `tools` entries of `type: "function"` (`name`, `description`, `parameters`, `strict`),
  plus `tool_choice` (`auto`, `none`, `required`, `{"type":"function","name":…}`) and
  `parallel_tool_calls`.
  - Translate them to chat `tools`/`tool_choice`/`parallel_tool_calls`.
  - Validate names (`^[a-zA-Z0-9_-]{1,64}$`), at most 128 tools, and parameters with the existing
    bounded schema check.
- Input items: accept `function_call` (`call_id`, `name`, `arguments`) and `function_call_output`
  (`call_id`, `output`). Translate them to an assistant message with `tool_calls` and to
  `role: tool` messages.
- Output: map chat `tool_calls` to `function_call` output items (`id`, `call_id`, `name`,
  `arguments`, `status`). This replaces today's `unsupported_output` refusal.
  - Streaming emits `response.output_item.added`, `response.function_call_arguments.delta`/`.done`
    and `response.output_item.done`.
  - Stored responses (R3) save and replay these items, so `previous_response_id` chains with tool
    calls work.
- Refuse tools for models whose catalogue entry has `tools: false`, using the same check chat uses
  (`takes_tools` / template caps in [loadcoach/app.py](../loadcoach/app.py)). Return 400 `"This
  model does not support tool calls."`.
- Requires `use-tools` (TA).

**Tests:**
- Round trip through a fake model that returns a tool call; the follow-up with
  `function_call_output`.
- Streaming argument deltas.
- A stored chain with tool items.
- A model without tools is refused; an account without `use-tools` gets 403.
- The `openai` SDK parses the output, if present.

## TC. Shared tool loop and server-run ToolLocker tools in Responses

**Depends on TA, TB and R3.**

### Shared tool module

Write one module used by both TC and TD (Tool chat), for example `turnstile/tool_loop.py`. It does
four things, always **with the caller's own key**:
1. **List** the caller's ToolLocker tools: `initialize` + `tools/list` on the control node's `/mcp`
   (optionally `/mcp/{toolset}`), and convert them to function definitions. MCP `inputSchema`
   becomes `parameters`; the MCP tool name stays the function name.
2. **Call** a tool: `tools/call`, with `X-LoadCoach-Trace` set to the request's trace and
   `X-LoadCoach-Sensitivity` set to its level, so ToolLocker applies its level rules and raises
   the trace.
3. **Convert** an MCP result (`content` parts, `isError`) to a model-ready tool message and to the
   exact output shown to people. Never alter the exact output. Bound the size sent to the model
   (e.g. 64 KiB, truncated with a visible note); keep the exact output separately.
4. **Run steps.** Model request → tool calls → results → model request, sharing one trace and
   stopping at the limits. It must support both automatic running (TC) and approval at each step
   (TD).

Reuse the existing ToolLocker client settings ([weightroom/app.py](../weightroom/app.py)
`tool_http`, and Turnstile's own ToolLocker routing). Don't add a new listener or credential.

### Responses `type: "mcp"`

- Accept `{"type":"mcp","server_label":"toollocker","server_url":<optional>,
  "allowed_tools":[…], "require_approval":"never"|"always"|{"never":{"tool_names":[…]}}|
  {"always":{"tool_names":[…]}}}`.
  - `server_url` must be omitted or be this the control node's own `/mcp` or `/mcp/{toolset}`. Refuse
    anything else with 400 `"Only this server's ToolLocker is available."`.
  - `allowed_tools` can only narrow the caller's list. Tools the caller can't see are ignored,
    not errors, so a hidden tool's existence isn't revealed.
- **Output items:**
  - `mcp_list_tools` (server label and the tools offered);
  - `mcp_call` (`name`, `arguments`, `output` or `error`);
  - `mcp_approval_request` when approval applies. The response then ends with
    `status: completed` and the pending request. The client continues with
    `previous_response_id` and an `mcp_approval_response` input item (`approval_request_id`,
    `approve`, optional `reason`). This needs `store:true` (R3). Refuse approval mode with
    `store:false` with a clear 400.
- **Streaming events:** `response.mcp_list_tools.in_progress`/`.completed`,
  `response.mcp_call.in_progress`, `response.mcp_call_arguments.delta`/`.done`,
  `response.mcp_call.completed`/`.failed`.
- Function tools (TB) and MCP tools may be mixed in one request. Function calls end the response
  for the client to run them, as normal.
- **Limits:** at most **25 tool calls** and **15 minutes** per request, as settings in the existing
  settings mechanism, editable on the Settings pages. On reaching either limit, end with
  `status: incomplete` and `incomplete_details.reason: "max_tool_calls"` or
  `"max_tool_time"`.
- **Sensitivity and cloud models:**
  - All model and tool calls in one response share one trace. If the trace's level rises above
    what the chosen model may receive, the next model request is refused by the existing hosted
    check.
  - Stop the response with `status: failed`, code `tool_result_too_sensitive`, message `"A tool
    result is marked LEVEL and may not be sent to the hosted model NAME."`.
  - Don't retry with another model.
- **Permissions:**
  - The request needs `use-tools` (TA).
  - Every tool call is checked again by ToolLocker under the caller's key. A tool removed or
    hidden mid-run fails that call; the model sees the failure text.
  - Not allowed in batches: extend R4's per-line checks so a batch line with an `mcp` tool gets a
    per-line error. Function tools (TB) are allowed in batch lines.
- **Records:** each tool call appears in AI Logs under the response's trace, as ToolLocker already
  records calls. Stored responses (R3) include `mcp_call` items with their output at the record's
  level.

### Tests

- Two accounts with different `tools`/`remove_tools` get different `mcp_list_tools`.
- An app-session key gets the intersection.
- `allowed_tools` can't widen the list.
- A foreign `server_url` is refused.
- A tool hidden mid-run fails at call time.
- Approval chain: request → approval request → approve → result; decline.
- Limits give `incomplete`.
- A sensitive tool result with a hosted model gives `tool_result_too_sensitive`, and nothing is
  sent to the hosted model (assert the fake hosted service received nothing).
- Streaming event order.
- Mixed function and MCP tools.

## TD. Tool chat page

**Implemented 2026-11-14:** caller-scoped tools, schema/raw argument forms, exact results, explicit model approvals, shared limits and cancellation, sensitivity-safe cloud continuation, separate private history with portable migration/backup/transfer, and shared Text chat/Settings tool components. Operating instructions are in [Tool chat](../weightroom/pages/docs/web-app.html).

**Depends on TA and the shared tool module from TC.**

**Place and access:**
- Route `/chat/tools`. Add it to [weightroom/navigation.py](../weightroom/navigation.py) under
  `Interact` → `Chat & analysis`, after `/chat`.
- Shown and allowed only when the signed-in person has `use-tools` and at least one visible
  ToolLocker tool. Otherwise the link is hidden and a direct visit gets the framed 403, as other
  pages do.
- **Try a tool** stays in Settings → Tools for `manage-settings`, unchanged.

**Layout and reuse:**
- Follow the Text chat layout and reuse its templates instead of copying them:
  - sidebar, history and rename: [chat_sidebar.html](../weightroom/pages/chat_sidebar.html),
    `rename_form` in [chat_parts.html](../weightroom/pages/chat_parts.html),
    [chat_sidebar.js](../weightroom/static/chat_sidebar.js);
  - model choices and the composer: [chat_main.html](../weightroom/pages/chat_main.html).
- Reuse the tool listing and call pieces from [tools.html](../weightroom/pages/tools.html) and
  [tools.js](../weightroom/static/tools.js), extracting shared templates where both pages need
  them.
- Record the new and extracted components in the AGENTS.md template index.

**Tools panel:**
- Lists the person's tools (from the shared module, with their own key), grouped by server, with
  each tool's description and parameter schema shown readably: name, type, required, description,
  enum values.
- Search over names and descriptions, and a tool-set choice as on Try a tool.

**Calling a tool directly:**
- Choosing a tool opens a form built from its parameter schema, with a raw-JSON fallback for
  shapes the form can't express.
- **Run** calls it through the shared module and shows the **exact** tool output: every content
  part, `isError`, and the raw JSON in a disclosure.
- The call and output are added to the conversation history.

**Chatting with a model:**
- A model picker restricted to models the person may use that support tools.
- Cloud models follow the same sensitivity rule as TC: show the refusal clearly when the
  conversation's level no longer allows the chosen model.
- The conversation sends the person's visible tools (or the chosen tool set) as function
  definitions.
- When the model proposes calls, the page shows each proposed call, with its tool, arguments and
  description, and three choices:
  - **Run** as proposed;
  - **Edit arguments**, then run;
  - **Decline** with an optional reason, which is sent to the model as the tool result.

  Each output is shown exactly, then sent to the model, which continues.
- Limits: the same 25 calls / 15 minutes per model turn as TC.
- Stopping or cancelling uses the existing chat request cancellation.

**Sensitivity:** the conversation has a trace and level, as Text chat does. Tool calls raise it
through ToolLocker. Saved messages and tool outputs keep their level; a person whose clearance is
later lowered can't open a conversation above it.

**History:**
- Saved in the existing private chat store (`weightroom.chat`, [db/schemas.py](../db/schemas.py)
  `CHAT`). Add a conversation kind or flag for Tool chat, with a migration, so Text chat and Tool
  chat lists stay separate.
- Store tool calls (arguments, approval decision, edits) and their exact outputs as message parts.
- Renaming, deleting and account-deletion cleanup work as in Text chat. Backup and transfer cover
  the new columns.

**Accessibility and states:**
- The keyboard can reach every step. Live regions announce proposed calls and results.
- Empty states: no tools visible, no tool-capable model, no history.
- ToolLocker unavailable and error states.
- Light and dark, and 390 px narrow layouts.
- The proposal panel survives polling and replacement (`data-hold-refresh` where needed).

**Tests:**
- Access:
  - hidden link and 403 without `use-tools` or without tools;
  - only the person's own tools are listed (two accounts);
  - Try a tool unchanged.
- Calls:
  - a direct call shows exact output, including an error result;
  - model loop: propose → edit → run → model continues; decline;
  - limits;
  - a sensitive result with a cloud model is refused and nothing is sent.
- History:
  - saved, renamed, deleted, separate from Text chat, and hidden after clearance is lowered;
  - SQLite and PostgreSQL migrations.
- The UI audit and all-page browser checks in
  [tests/test_ui_consistency.py](../tests/test_ui_consistency.py), including narrow, empty and
  error states.

## Order and reporting

Do them in order: **TA → TB → TC → TD**. TA has no dependencies and may be done before R1.
Report after each milestone as described in [openai-api-work.md](openai-api-work.md#order-and-reporting).
