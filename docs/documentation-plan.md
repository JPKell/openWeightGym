# Documentation, docs tools and help agent plan

Plan date: 2026-11-14, revised the same day after an interview with the owner. Written for an
implementing agent working in this repository.

There are three deliverables, in this order:

1. **Complete documentation**: every page, workflow, API, setting and module in the Docs library (`/docs`).
2. **Docs MCP tools**: built-in ToolLocker tools that search and read that same documentation.
3. **Help agent**: an **Ask** mode in Docs, also usable by MCP and API clients, that answers
   from those tools and cites its sources.

The overriding goal is **cheap upkeep with no duplicated effort**:
- every fact lives in exactly one place;
- reference material is generated from code;
- a test, not a person, notices missing documentation.

Record progress, approvals and open items in [agent-status](agent-status.md), not here. Follow
[agents.md](agents.md) and the repository [AGENTS.md](../AGENTS.md). Deploy only when the owner
asks, and then only with `owg update; owg restart`.

---

## Owner decisions (2026-11-14)

| Topic | Decision |
|---|---|
| Format | Hand-written guides are **Markdown only**. Jinja templates remain only for generated reference pages and the library shell. |
| Catalogue | **Folder + front matter**: `docs/guide/<section>/<topic>.md`. Adding a file adds the page. The hand-kept `SECTIONS` list goes away. |
| Plan and history files | **Left untouched and frozen** (`*-plan.md`, `*-work.md`, `loadcoach.md`, sweeps, evidence). No pointer lines are added. Guides are written fresh and are the only maintained source of current behaviour. Guides never include or quote plan files. |
| `docs/README.md` | Becomes a **short index of the guides**. Its "Use the app", "Where things run" and settings content moves into guides. |
| Generated pages | **API endpoints, settings reference, abilities and the page → guide map** are all generated from code. |
| Screenshots | **None.** Text and exact UI labels only. |
| Enforcement | **Coverage test**: it fails when a navigation page, API route, ability or setting has no guide. There is no changed-together check. |
| Who updates | **Same commit as the change**, written into AGENTS.md beside the template-index rule. |
| Gap log | When Ask can't answer, the **question text only (no account)** goes into a short list administrators see in Docs. |
| API reference visibility | **Administrators only** (`manage-settings`), as today. Public Web app guides keep the API keys topic. |
| Tool permission | Ask and the `docs__*` tools **require `use-tools`**. Accounts without it keep plain Search. |
| Help agent sources | Guides, generated references and Code references. **Administrators also get the `code__*` tools** when their tool access offers them. There is no FAQ or separate knowledge base. |
| Evaluation | A **small smoke set** (about 10 questions, fake model) plus the gap log. There is no large question set to maintain. |
| Per-page help links | **No.** Docs are reached from the Docs menu. |
| Ask history | **Not kept.** The conversation lives in the browser tab only, and usage appears in the normal request log. |
| Help model | The role **`docs-help`**, chosen by an administrator in Roles. Ask explains how to set it when it is missing. |

## Where each fact lives

This table is the no-duplication contract. Put it in [agents.md](agents.md#documentation-maintenance)
when M1 lands.

| Kind of fact | Single home | Shown on `/docs` by |
|---|---|---|
| How to use a page or feature (users and administrators) | `docs/guide/<section>/<topic>.md` | Discovery from front matter |
| How to install, update or operate | Existing maintained files ([installation.md](installation.md), [operations.md](operations.md) sections, package READMEs such as [toollocker](../toollocker/README.md), [voice](../voice/README.md), [plugins](../plugins/README.md), [benchmarks](../benchmarks/README.md)) | A guide stub whose front matter `include`s the file or named sections. Nothing is copied. |
| Endpoint list, methods, abilities and summaries | Route declarations and handler docstrings | Generated API endpoints page |
| Setting keys, defaults and meanings | `settings/defaults/*.toml` comments and [setting_labels.py](../weightroom/setting_labels.py) | Generated settings reference |
| Abilities and what they allow | Ability definitions (as used by [abilities.html](../weightroom/pages/abilities.html), `loadcoach/users.py`) | Generated abilities page |
| Which guide covers which page | Each guide's `covers` front matter, checked against `NAV_SECTIONS` | Generated page → guide map and coverage test |
| Module, class and function docs | Docstrings, under the `SOURCES` allowlist | Code references (unchanged) |
| History, decisions and evidence | Plans and logs (frozen) | Not shown as guides |

Rules that follow from it:
- **No fact is written twice.** When a guide needs a fact that has a generated home (a default,
  a limit in settings, an ability name), it links to the generated entry instead of restating it.
- A limit or behaviour that exists only in code goes into the guide, which is its home. The
  code's docstring may hold the same contract for developers. If they disagree, fix the docstring.
- `SOURCES` stays a hand-written allowlist **on purpose**: it is a security boundary, not documentation.

---

## Part A: Documentation

### A1. Guide file format

Front matter is TOML (parsed by the standard library `tomllib`, no new dependency) inside a
leading HTML comment, so it is invisible on GitHub and on `/docs`:

```markdown
<!--docs
title = "Model catalogue"
summary = "Find, add and remove models, and see which nodes hold each copy."
order = 10
covers = ["/models", "/models/images", "/models/audio"]
aliases = ["/docs/modules/models"]          # optional: old addresses that redirect here
# include = "../../installation.md"         # optional: show a maintained file instead of a body
# parts = ["Install", "Uninstall"]          # optional: only these ## sections of it
-->
# Model catalogue

One sentence saying what the page is for.
...
```

- **Section metadata:** each section folder has an `_section.md` with `title`, `summary`,
  `order` and `audience` (`"public"` or `"admin"`). Its body is the section overview.
- **Admin-only paragraphs:** a public guide may hide a block from other accounts with
  `<!-- audience: admin -->` … `<!-- /audience -->`. Accounts without `manage-settings` see
  nothing there, and search does not index it for them. On GitHub the block is simply shown.
- **Topic skeleton** (leave out any heading with nothing under it):
  1. One sentence: what it is for.
  2. **Before you start**: abilities, services or models it needs, linking to generated abilities entries.
  3. **Do it**: numbered steps for the 1–3 main tasks.
  4. **Options**: a table of the controls that are not obvious.
  5. **Limits and troubleshooting**: real limits, and the messages users actually see.
  6. **Related**: links to other topics.

  Keep each topic under about 120 rendered lines, and split anything longer.
- **Style:** plain words, active voice, UI labels exactly as rendered in **bold**, and `code` for
  routes, keys and commands. No screenshots, no milestone IDs, no history.
- **Verify every claim** against the source before writing it, and name the source files in the
  commit message. Current code wins over anything in a plan.

### A2. Discovery and rendering (M1, engineering only, no content)

1. Teach [system_docs.py](../weightroom/system_docs.py) to build `SECTIONS`, `BY_SLUG`, `MOVED`
   and `DOC_PAGES` by scanning `docs/guide/`. Keep these working unchanged:
   - the output shape, so `docs_views.py`, the templates, search, previews and Code references keep working;
   - `visible()`, which now uses `audience`;
   - revision caching (`_written()`);
   - link rewriting (`_links()`).
2. Add front-matter parsing and the admin-block filter to [doc_markdown.py](../weightroom/doc_markdown.py).
   Search must index the filtered text for each audience.
3. Make `include`/`parts` work for maintained files. Refuse an `include` that points to a plan or
   history file (`*-plan.md`, `*-work.md`, `loadcoach.md`, `application-sweep-*`, `agent-status.md`)
   so the frozen-plan rule is enforced by code.
4. Turn each existing topic into a guide file:
   - Each `weightroom/pages/docs/**/*.html` topic template becomes Markdown, and the template is deleted.
   - Each existing `doc=`/`parts=` topic that pulls from a *maintained* file becomes an `include` stub.
   - Topics that pull from *plan* files (Classifier, Image extraction) get fresh guides written
     from the current code. The plans themselves are not edited.
   - Generated pages (Custom endpoints, Headers if generated, Code references) stay as templates.
5. Keep every current URL working. Existing `MOVED` entries become `aliases` in the matching guides.
6. Update the docs previews (`scripts/preview-docs-designs.py`, `tests/test_docs_designs.py`) to
   read the discovered catalogue.

### A3. Coverage test (M1)

- Add `tests/test_docs_coverage.py`. It fails when any of these lacks a guide:
  - every route in `NAV_SECTIONS` and the account links in [navigation.py](../weightroom/navigation.py) must be in some guide's `covers`;
  - every public `/v1` and `/owg` route must appear on the generated endpoints page, which means
    each handler needs a docstring summary;
  - every ability must have a description;
  - every key in `settings/defaults/*.toml` must have a comment or label.
- Start with an explicit allowlist of today's gaps. Each content batch shrinks it, and after M2
  it must be empty and then deleted.
- Add `scripts/docs-coverage.py` to print the same report for humans.

### A4. Generated reference pages (M1–M2)

| Page | Built from | Where |
|---|---|---|
| API endpoints | Route declarations, handler docstrings, required ability | Extends the existing Custom endpoints reader |
| Settings reference | `settings/defaults/*.toml` with comments, `setting_labels.py` | Deployment and operations section. **Keys and defaults only, never values from live `settings/*.toml`.** |
| Abilities | Ability definitions | System reference section |
| Page → guide map | `NAV_SECTIONS` + `covers` | Docs home, "Find the guide for a page" |

Each page is cached on its source files' revisions, like `_written()`. Missing docstrings and
comments are fixed **in the code**, never patched on the page.

### A5. Target structure

Section folders under `docs/guide/` (new topics marked ★). Headers keeps its current page.

| Section (audience) | Topics |
|---|---|
| `getting-started` ★ (public) | What openWeightGym is; Find your way (navigation, search); Your first chat |
| `web-app` (public) | Chat; Voice chat ★; Classifier; Image generation ★; Image extraction; Speech ★; Transcribe ★; Music and sounds ★; Voice desktop app ★; Collections; Workflows; Tools; API keys; My account ★ |
| `models` ★ (admin) | Model catalogue; Model configuration and presets; Image models; Audio models; Cloud APIs; Roles and routing; Dry fit; Files; Fingerprints; Downloads |
| `benchmarks` (admin) | How a run works; What is measured; Scores and results; Node measurements ★; Compare ★; Manage scoring ★; Files and operation |
| `monitoring` ★ (admin) | Usage and costs; Reliability; Model requests; Batches; Stored responses; Event log; Plugin logs; System logs; Status; Telemetry; Alerts |
| `administration` ★ (admin) | General settings; Compute nodes; Users; Groups; Allowances; Tools; Plugins; Media settings; Voice telephone and status; Schedules; Components |
| `system` (admin) | Models; Accounts and groups; Abilities ★ (generated); Logs; Hosted providers |
| `api` (admin) | Authentication and keys ★; OpenAI compatibility; Custom endpoints (generated); Headers; Errors and limits ★; MCP endpoint ★; Examples ★ |
| `data` (admin) | Stores and ownership; Backups and restore; SQLite and PostgreSQL |
| `operations` (admin) | Installation (include); Where things run; Settings and Settings reference ★ (generated); Node installation and updates (include); Troubleshooting ★ |
| `modules` (admin) | One include stub per package README, plus a short summary for packages without a README |

Add the new sections to the `'Docs'` group of `NAV_SECTIONS`. Then reduce
[docs/README.md](README.md) to a short index linking to each section. Update the about ten links
elsewhere that point at its anchors (for example in [AGENTS.md](../AGENTS.md#template-index))
so they point to the guides.

### A6. Content batches (M2)

Do one batch per commit. Each commit ships its guides, shrinks the coverage allowlist and passes A7.

1. Getting started, plus expanded Chat, API keys and Tools. Check them with a non-admin account.
2. The rest of the Web app section.
3. Models.
4. Monitoring.
5. Administration.
6. API additions. Run each example request against a local instance, and make sure its output matches what the guide shows.
7. Benchmarks additions, Troubleshooting and the `docs/README.md` index. The allowlist must be empty at the end of this batch.
8. Editorial pass: consistent terms (control node, compute node, role, ability), length limits, summaries, cross-links.

### A7. Verification for every batch

- `rtk proxy .venv/bin/pytest tests/test_documentation.py tests/test_docs_library.py tests/test_docs_coverage.py tests/test_source_docs.py tests/test_doc_markdown.py tests/test_docs_designs.py`
- `tests/test_docs_browser.py` and the docs/all-page checks in `tests/test_ui_consistency.py`, at desktop and narrow widths, in light and dark.
- Permission check: an account without `manage-settings` sees only public sections, never sees
  admin blocks, and search returns no admin text.
- `rtk git diff --check`. Every link and anchor resolves.

### A8. Keeping it current (written into AGENTS.md at M1)

- A change that adds or alters a page, route, ability or setting updates its guide **in the same
  commit**. The coverage test catches new things that have no guide.
- Generated pages need no edits. Keep handler docstrings and settings comments accurate instead.
- Never update plan or history files to describe current behaviour. Update the guide.
- Check the gap log (Part C) when choosing documentation work.

---

## Part B: Docs MCP tools (M3)

### B1. Design

Add a built-in, caller-bound tool server **`docs`** to ToolLocker that follows `ExtractionTools`
in [extraction.py](../toollocker/extraction.py):

- ToolLocker forwards each call to WeightRoom with the **signed proof of the actual caller**.
  WeightRoom applies the same rules as `/docs` (`audience`, admin blocks, `manage-settings`).
  The tools never read files themselves, so visibility is enforced in one place.
- Enabled automatically when the console URL is configured (as `extraction_url` is) and no
  operator-defined `docs` server exists.
- Offered as tool set `docs` (`/mcp/docs`). Access follows the normal ToolLocker rules: the
  account's tool patterns, sensitivity and tool set. Set its level to `normal`.
- Output is bounded (for example 8,000 characters per read, 10 search hits), and every result
  carries its canonical `/docs/...#anchor` URL for citation.

WeightRoom JSON endpoints, in [docs_views.py](../weightroom/docs_views.py). They reuse `search()`/`article()` and the generators rather than duplicating them:

| Endpoint | Returns |
|---|---|
| `GET /owg/docs/catalogue` | Visible sections and topics: title, summary, URL, covered pages |
| `GET /owg/docs/search?q=&scope=&limit=` | Hits: title, URL#anchor, heading, snippet |
| `GET /owg/docs/article?url=&anchor=&offset=` | Plain-text article or one section, paged |
| `GET /owg/docs/endpoints?q=` | Matching routes from the generated endpoints data (admin) |
| `GET /owg/docs/settings?q=` | Matching settings reference entries: key, default, meaning (admin) |
| `GET /owg/docs/abilities?q=` | Matching abilities (admin) |
| `GET /owg/docs/source?module=&symbol=` | Module outline or one symbol, under `SOURCES` (admin) |

Tools: `docs__list_topics`, `docs__search`, `docs__read`, `docs__find_endpoint`,
`docs__lookup_setting`, `docs__explain_ability`, `docs__code_outline`, `docs__code_symbol`.

### B2. Security

- All tool output is data. Text inside docs or code never changes the agent's rules.
- Source reads go only through `SOURCES`. Settings come only from `settings/defaults`. Never
  return keys, tokens or live settings values.
- Calls are recorded in the existing tool event log, with the caller's identity.

### B3. Tests

Add `tests/test_docs_tools.py`, modelled on `test_toollocker.py`/`test_tool_access.py`. It covers:
- tool listing per tool set;
- the permission matrix (no access, user, admin);
- refusal of forged or missing caller proof;
- bounded output;
- path-like input rejection;
- citation URLs that resolve to real pages.

---

## Part C: Help agent (M4)

### C1. Behaviour

- **One agent, three ways to reach it:**
  1. The **Ask** tab on `/docs`, beside Search.
  2. The role `docs-help` on `/v1/responses` or `/v1/chat/completions`.
  3. External agents connecting to `/mcp/docs` with their own key, for example
     `claude mcp add --transport http owg http://CONTROL:8443/mcp/docs --header "Authorization: Bearer KEY"`.
- **Implementation:** a server-side Responses request with an `mcp` tool restricted to the `docs`
  tools. For administrators whose tool access offers `code__*`, those tools are added too. The
  model is the role `docs-help`, and the request runs under the **caller's own** key/session, so
  model permissions, `use-tools`, allowances and usage accounting apply unchanged. Reuse
  [response_tools.py](../turnstile/response_tools.py). Do not write a new agent loop.
- **Instructions** live in one file, `weightroom/docs_help_prompt.md`, and say:
  - search before answering;
  - state only what tool results support, and cite each factual sentence with its docs URL (or file and symbol for code);
  - prefer guides and generated references, and treat the code as the final authority;
  - when nothing covers the question, say so and point to the nearest topic;
  - for admin topics the caller can't read, say an administrator can help;
  - never guess routes, settings or ability names.
- **Gap log:** when the agent's answer is "not documented" (the agent signals this with a fixed
  marker the server detects), keep the question text, a count and the last-asked time. Store
  **no account and no answer**. The list is bounded (for example 200 entries, deduplicated on
  normalized text, 500 characters each) and owned by the control node. Administrators see it as a
  **Gaps** view in Docs, where they can dismiss entries. Say in the My account privacy guide that
  unanswered question text is kept anonymously.

### C2. UI

- Ask reuses `docs_library_parts.html` (search form, result rows for citations) and `parts.html`
  notices. Add any new component there and to the **Template index** in [AGENTS.md](../AGENTS.md#template-index).
- It uses a native form POST with an HTMX streaming fragment. Without JavaScript, the full answer
  renders when complete. A draft question survives a refresh (`data-hold-refresh`). The
  conversation is kept only in that browser tab.
- States to cover:
  - the account lacks `use-tools` (Search only, with a short note);
  - no `docs-help` role is set (administrators see how to set it; others are told to ask an administrator);
  - ToolLocker is offline (fall back to Search);
  - model error, empty answer, and out of allowance.

### C3. Evaluation (M5)

- `tests/test_docs_help.py` with about 10 fixed questions and a fake model. It checks:
  - the tool loop and that citations are present;
  - the "not documented" path writes the gap log without the account;
  - a non-admin cannot get admin text or code;
  - a caller without `use-tools` is refused.
- Real quality is tracked through the gap log. There is no large question set to maintain.

---

## Milestones

| # | Deliverable | Done when |
|---|---|---|
| M1 | Guide discovery, front matter, admin blocks, include stubs, existing topics converted, generated references, coverage test with allowlist, AGENTS.md/agents.md rules (A1–A4, A8) | All existing URLs still work, A7 passes, and the allowlist lists today's gaps |
| M2 | Content batches 1–8 (A5–A6) | Allowlist empty and deleted, `docs/README.md` is an index, A7 passes |
| M3 | Docs JSON endpoints and `docs` MCP tools (Part B) | B3 passes, and **Settings → Tools → Try a tool** works for each tool |
| M4 | `docs-help` role, Ask panel, prompt and gap log (C1–C2) | Ask answers with citations for user and admin accounts, and every state renders |
| M5 | Smoke tests (C3) and the first gap-log review | Tests pass, and the first gaps are recorded in agent-status |

M3 can start after M1, in parallel with M2.
