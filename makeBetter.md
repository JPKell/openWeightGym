# Usability plan for v1 (2026-10-30)

## Context

The UI consistency plan (U0–U7) is deployed. Before locking the console into a v1 release, the owner asked for further usability improvements so that users have a good experience. This plan lists sixteen findings, V1–V16. V1–V6 are the must-fix items for v1; V7–V16 are polish and clarity. Progress is recorded under [Execution](#execution).

Evidence: a read-only headless probe (Playwright from `.venv`, `--disable-gpu`, the tests' fakes, not the live console) of all 91 console routes at 1440 wide, key pages at 390, four missing-object and unknown URLs, six pages as the plain account `pat`, and two signed-out visits. It recorded page titles, unlabeled and placeholder-only fields, unconfirmed destructive buttons, raw values, program names in page text, empty tables and error responses; screenshots of key pages were reviewed. Script and baseline: `data/qualification/usability/usability_probe.py`, `before.json` and `before-shots/` (local, `data/` is ignored). Run it with `.venv/bin/python -m pytest data/qualification/usability/usability_probe.py -p no:cacheprovider`.

Because the probe ran on fakes, findings that depend on live data (empty tables, raw values, the image pages) must be confirmed on the live console before and after each fix.

## Must fix before v1

### V1 — Error and refusal pages
Baseline findings (implementation status below):
- An unknown URL (for example `/no-such-page`) returns Starlette's plain-text "Not Found": no console frame, no navigation, no way back.
- A missing model, request or benchmark run renders `refused.html` with "Not here — This page is unavailable to the current account." That wording describes a permission refusal, not an object that does not exist.
- On a permission refusal (`pat` opening `/settings`, `/models`, `/users`, `/deploy`), the red `said_box` message overlaps the "Not here" card title.
- The sidebar falls back to the Logs section for refused pages (`navigation.py` uses `groups[3]` when the active link is not in any visible group).
- Fix: one error template with three cases: not found ("There is no model named X"), not permitted (name the ability needed), and something went wrong (500, with the request ID). Register it for Starlette 404/500 so every unknown URL gets the console frame. Keep the requested section highlighted, or none, never an unrelated one. Lay the message out inside the card, not over it.

### V2 — One confirmation pattern for destructive actions
- Today there are three behaviors: the two-click "Are you sure?" (`data-confirm-two`, 8 forms: schedules, deploy, files, status), a "Yes, remove X" checkbox (8 forms: model, user, group, role, cloud API, machine actions, benchmark import), and none.
- Unconfirmed today: Clear queue (`benchmark_queue_card.html`), Stop all (`batches.html`), Rotate now for the machine key (`compute_nodes.html`), Delete collection, Delete collection and sources and Reset embedding space (`collections.html`), Delete workflow and saved outputs (`workflows.html`), Remove group admin (`group.html`), Disable model (`cards.html`), Disable user (`user.html`).
- Fix: two levels, both from shared pieces in `parts.html`. Reversible or interrupting actions (stop, clear queue, disable, unload) use the two-click confirmation. Actions that lose data or credentials (delete, remove, reset embedding space, rotate key) use the "Yes, …" checkbox inside a danger card. U7-style test: every submit button whose label is a destructive verb carries one of the two.

### V3 — Feedback when a button is pressed
- No form disables its button or shows progress. There is one `hx-indicator` in all templates and no `.htmx-request` styling.
- Slow actions (Add model copies the file, start a benchmark run, downloads, deploy, backups) look dead and can be submitted twice.
- Fix: one delegated script in `console.js`: on submit, disable the submitter and change its label to "Working…"; restore it on `pageshow` (back/forward cache) and after an htmx response. Style `.htmx-request` once in `style.css` for the htmx parts.

### V4 — Sign-in keeps the destination and speaks plainly
- `welcome()` (`weightroom/app.py`) always redirects to `/`. A signed-out person who opens a shared link such as `/models/sample` sees the sign-in form at that URL, signs in, and lands on Home.
- When the sign-in service is down, the form shows "Turnstile is not answering, so nobody can be signed in. Start it: systemctl --user start turnstile". That is operator instruction shown to every user.
- The form says "Sign in to your Suite account." while the header says WeightRoom (see V7).
- Fix: carry the requested path in a hidden `next` field, accept only same-site relative paths, and redirect there after sign-in. Users see "Sign-in is unavailable right now. Try again in a minute, or tell your administrator."; the systemctl hint goes to the log and the Status page.

### V5 — Navigation on phones and the collapse control
- At 390 wide, the top tabs scroll sideways; System, Settings and Docs are off-screen with no hint.
- The section links (Collections, Workflows, Music, …) are reachable only from a "»" tab that floats over the content halfway down the screen.
- On desktop, the « collapse button floats mid-sidebar (about 550px down) on every page. In Chat it splits the conversation list from the other Chat links, which are pushed below a gap.
- Fix: on phones, one menu button in the header opening an offcanvas panel with both levels (sections and their pages). On desktop, pin the collapse button to the bottom of the sidebar. In Chat, keep the section links above the conversation list, or move the list into its own card in the page.

### V6 — Chat shows which model will answer
- The model picker is inside the closed "Options" disclosure (`chat_main.html`), so the person cannot see which model will answer before pressing Send.
- The message box has only placeholder text ("Ask something") and no label.
- There is no message for an account with no permitted models (not verified live).
- Fix: show the model picker (or the chosen model as a button that opens it) beside Send; keep sampling and comparison in Options. Add `aria-label="Message"`. When no model is available, say so and say who can grant one.

## Polish and clarity

### V7 — One product name and no program names in user text
- Program names appear in the page text of 26 pages: for example "Turnstile may place this model on any enabled node" (model page), LoadCoach on Status and Deploy, Turnstile/LoadCoach/Spotter on Compute nodes, ToolLocker on Tools and Groups, SoundRoom on Voice, WeightRoom on Alerts, Schedules and Audio.
- The product is called WeightRoom in the header and page titles, and "Suite" on the sign-in page and in the guide.
- Fix: the owner chooses one user-facing name. Outside Docs and administrator diagnostics, replace program names with role words: "the gateway", "the compute node", "the tool service", "the monitor".

### V8 — Wording pass
- Examples: "Nobody holds the cards for themselves. This is primary compute." (Status, `status_nodes.html`), "Every fingerprint there has been" (`models.html`), "Not here" (`refused.html`), "Back to the first page".
- Fix: one pass over page leads, card hints, empty-state text and button labels with a short rule in `docs/agents.md`: plain words, say what the person can do, no metaphors.

### V9 — Raw values
- "None" appears on Backups, the model page and Images.
- System logs (`/output`) shows an unformatted timestamp (`2026-10-08T10:00:00`).
- Token totals show as "10.0" and "0.0" on Home and Usage, because `usage_kpis.html` formats counts with the benchmark `number` macro.
- Fix: a formatting rule for missing values ("—" or a short phrase), one shared time format, and whole numbers with thousands separators for counts. The U7 consistency test gains a check for visible `None`, `null`, `NaN` and ISO timestamps.

### V10 — Times in the person's zone
- Twenty templates state UTC; request tables show times with no zone at all.
- Fix: show times in the browser's local zone (or an Appearance setting), label the zone once per table, and keep the UTC value in the `title`. Usage day boundaries stay UTC and say so.

### V11 — Empty tables say they are empty
- Tables render only their header row when empty on Audio models, System → Voice, Compute nodes, a benchmark run's detail, the model page's Adapters table, and Images.
- Fix: a shared empty-row piece, as Status already does ("No requests right now."), with a next step where one exists ("Add an adapter").

### V12 — Models page order
- The "Add a model" form sits above the list of models; the list is the page's main content.
- "compute · unknown" beside a "Show node" button does not say what is unknown or what the button does. The Add button is disabled with only a separate notice explaining why.
- Fix: list first; "Add a model" as a closed card or a header action. Name the node state plainly ("compute · not reachable"), rename "Show node" to what it opens, and explain a disabled Add next to the button.

### V13 — Home starts with what the person can do
- Home shows only usage. A plain user lands on zero token counts, scope buttons named after groups (Admin, Front, Plain, Superuser) and four cost cards including "Mock fallback" and "Model-rate estimate".
- Fix: Home leads with a Start a conversation action and the person's available models and their state, then usage. Label the scope buttons ("Group: Admin") and show the detailed cost breakdown only on the Usage page.

### V14 — Help for everyone
- Docs is visible only with `manage-settings` and is mostly a source-code reference (module lists and definitions).
- Only 21 of 91 pages link to any help.
- Fix: a short user guide visible to every account (chatting and options, collections, workflows, voice, API keys and the model API address). Link to it from the page leads of those pages. The source reference stays administrator-only.

### V15 — Visible connection loss
- 13 regions refresh themselves. When the console server restarts or the network drops, they fail silently and show stale data.
- Fix: handle `htmx:sendError` and failed polls in one place: show a small "Connection lost — retrying" bar, and remove it on the next successful response.

### V16 — Fields labeled only by placeholder
- Placeholder-only fields on 12 pages: Requests (six filters), Users (two), Chat and its studio pages (Music, Sounds, Speech), HuggingFace, Fingerprints, System logs, Status ("Reason"), the model page (two) and the plugin instance page ("Underlying profile/model ID").
- Fix: a visible label, or `aria-label` where a toolbar has no room for one. The U7 consistency test gains a check that every visible field has an accessible name.

## Also before v1

- Run the owner walkthrough (`app-walkthrough.md`, every check still "Not tested") on the live console with a non-administrator account after V1–V6.
- Check Chat → Image generation and Models → Image workflows live: in the fakes both pages return 200 but show "Internal server error" text.

## Working decisions

- V7: use Suite as the user-facing product name.
- V10: use the browser’s local time zone.
- V13: lead Home with Start a conversation and the person’s permitted models.

These defaults were stated when the owner authorized uninterrupted execution; they are implementation choices, not a claim of separate owner acceptance.

## Execution

Execute in order, V1 first. The owner approved continuing V2–V16 without milestone stops on 2026-10-30. Presentation, wording and small UI scripts only: POST field names, handlers and permissions stay unchanged, except V4's `next` field. Each milestone adds or extends a check in `tests/test_ui_consistency.py` or the related page test, reruns the probe against `before.json`, and reviews screenshots at 1440 and 390 in light and dark. Chrome only with `--disable-gpu`; check the benchmark queue before and after.


### V1–V16 delivery (2026-10-30)

All implementation milestones are complete and deployed at
[Suite](https://10.77.10.42/). The reviewed runtime and rollback contract are recorded in
[Installation](docs/installation.md#usability-release-2026-10-30); physical/subjective owner
acceptance is separate from these checks.

| Item | State |
|---|---|
| V1 | Done: status-specific framed errors, persistent reasons, request IDs and recovery links; correct navigation, escaped names, API JSON and Allow headers retained. Forty desktop/mobile/light/dark error cases passed. |
| V2 | Done: shared two-click/checkbox confirmation, including extra key/preset/conversation/media deletion controls; browser submission and full-route action audit passed. |
| V3 | Done: serialized submit values are preserved, duplicate submissions blocked, and progress restores after htmx failure or browser history restoration; focused browser checks passed. |
| V4 | Done: safe destination preserved through sign-in, plain outage text and administrator troubleshooting; redirect tests passed. |
| V5 | Done: complete phone menu, bottom desktop collapse control and section links above Chat history; keyboard/responsive tests passed. |
| V6 | Done: visible answering-model picker, message label and no-model guidance; browser checks passed. |
| V7–V8 | Done: Suite user-facing name and plain wording; service/cookie/preference identities and technical diagnostic references retained. |
| V9–V10 | Done: missing-value/count formatting, escaped log timestamps, browser-local times, UTC titles and filter conversion preserving handler semantics; timezone tests passed. |
| V11 | Done: shared empty rows and next steps, including dynamically edited model tables; placeholder transition tests passed. |
| V12 | Done: catalogue before closed Add form, plain node state and an adjacent disabled-action explanation; focused browser checks passed. |
| V13 | Done: Home starts with Chat and caller-permitted models/state; detailed costs remain in Usage. |
| V14 | Done: user guide available to every signed-in account, task-page links and standard model API address; source reference remains restricted. |
| V15 | Done: polling/stream failures remain visible until their own recovery; completed Chat replies retry failed refreshes without losing streamed text. |
| V16 | Done: remaining unnamed Chat/plugin fields labeled and all-route accessible-name checks added. |

Per-milestone probe and screenshot receipts live under `data/qualification/usability/vN/`; the live baseline is `data/qualification/usability/live-before/live.json`. Both image pages returned 200 with no internal-error text in that live baseline.

The final isolated probe reports no unnamed fields, raw display values, empty table bodies or JavaScript errors. Its earlier image-page failure was a fixture lifecycle omission: the fake gateway did not initialize the authenticated internal image/audio loopback. The fixture now matches that production wiring without starting background polling or inference; no production error was concealed.

Related improvements found during verification include responsive Status controls, image/audio node wording, cache revisions for changed scripts, fragment-safe post-login destinations and a populated-Chat retry layout that fits phones. Regression assertions now cover the intended copy, hidden mobile navigation, shared confirmations and semantic time markup.

Final local qualification: 127 related regression checks, 33 UI checks (including six route/theme/width sweeps), 23 media checks and 7 collection/confirmation checks passed. Logs are `data/qualification/usability/final-regressions.log`, `final-ui-tests.log`, `media-tests.log` and `collection-tests.log`. Collection status refreshes also retain the correct confirmation requirement as an action changes from Cancel to Reindex.

Activation receipts are `data/qualification/usability/activation.json`, `first-activation.json` and `second-activation.json`; only the front console changed runtime. A live non-administrator check exposed a System logs link leading to a transient 400 permission message. The UI now hides that unavailable link and returns a persistent framed 403 for direct visits, matching the existing service permission. Roles benchmark help also uses plain missing-answer wording.


Live review also fixed polling recovery after an htmx source replaces itself, released completed form feedback tied to the original request, and extended browser-local time formatting to telemetry chart axes/tooltips. Follow-up permission, recovery and chart checks passed (`final-fixes.log`, `final-runtime-fixes.log`). Two stale assertions from the earlier component redesign were reproduced on unchanged HEAD and updated to the shared-component structure.

The non-administrator [walkthrough findings](app-walkthrough.md#automated-live-review) retain the model-response issue and unperformed device/inference checks. These are explicit qualification limits, not unimplemented UI milestones or owner acceptance.


Final live qualification: all 58 authenticated pages and 232 desktop/phone light/dark cases passed with no layout, field-name, raw-value, JavaScript or HTTP 5xx failures. All 16 changed served assets matched the reviewed release. The seven media pages also passed 28 checks after their initial API requests settled; both image routes remained free of internal-error text. Browser-local chart times passed in Vancouver/Tokyo, and actual offline/recovery polling cleared its warning after source replacement. Receipts: `data/qualification/usability/live-qualification/live.json`, `live-media-ready/result.json`, `live-chart-zones.json` and `connection-diagnostics.json`.

Final health: WeightRoom remains active, the benchmark queue and active Chat table are empty, the prior benchmark pause is restored, and both temporary verification sessions were revoked. Scratch-data cleanup and the remaining walkthrough limits are recorded in the linked walkthrough; no host power/wake or physical-device test was performed. Final receipt: `data/qualification/usability/final-health.json`. Local commits are retained; nothing was pushed.
