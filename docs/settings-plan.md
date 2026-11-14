# Settings review and work plan (2026-11-14)

This plan reviews every setting the services read, says where each one should be changed, and
sets out the work to get there. It covers three things:

1. the settings the site already lets you change, and the restart-only ones that could apply at once;
2. the read-only machine values, and which of them could safely move to the site;
3. settings that are missing, compared with similar self-hosted AI platforms.

Sources: the defaults and example files in `settings/`, the config readers
([loadcoach/config.py](../loadcoach/config.py), [turnstile/app.py](../turnstile/app.py) `Settings`,
[weightroom/app.py](../weightroom/app.py) `Settings`, [spotter/app.py](../spotter/app.py),
[voice/service.py](../voice/service.py), [voice/gateway.py](../voice/gateway.py)), the settings
contract in [loadcoach/settings.py](../loadcoach/settings.py) and
[turnstile/settings.py](../turnstile/settings.py), hard-coded limits found in the code, and the
live pages on the control node (General, Compute nodes `jpk1`, Voice, Audio, Image) as of 2026-11-14.

## 0. Owner decisions (interview, 2026-11-14)

These answers are final. Where they differ from later sections, this section wins. The agent
should not need to ask the owner anything else. Items marked *(agent default)* were not asked;
use them as written.

### Scope and policy

| # | Topic | Decision |
|---|---|---|
| 1 | Node `keep_days` (loadcoach) | **Retire it** on nodes with `front` set. Remove it from the site and from `CHANGEABLE` for those nodes, and ignore existing overrides (leave the rows; the catalogue marks the key retired). The node's local copy stays bounded by `SENT_KEPT`. A standalone node (no `front`) owns its own log, so it keeps `keep_days` as today. The control node's `[turnstile] keep_days` becomes live on Retention and privacy |
| 2 | Allowances | Modes **advisory / warn / block**, default advisory. Block refuses **hosted requests only**: 403, error code `allowance_exceeded`, with a message naming the allowance and when it resets. Warn adds an `x-suite-allowance` response header and a personal spending notice at the alert threshold *(agent default)*. `allowance_period` offers rolling 24 h and calendar month, defaulting to today's behaviour |
| 3 | Remote MCP servers | Site-editable on Tools **by superusers only**. URL servers with a write-only key or `public = true`. Stored on the control node and merged with the file's servers; file entries win on a name clash. Command servers stay file-only |
| 4 | Alerts settings page | **Settings → Platform**, at `/settings/alerts`. The Alerts desk links to it |
| 5 | Fleet defaults | **No.** Per-node settings stay per node. The Requests page shows each node's value and links to Compute nodes |
| 6 | Routing and batch tunables | Editable in a collapsed **Advanced** section of Requests and queue, with today's constants as defaults and validated ranges |
| 7 | Moved node items | **All four:** per-node Wake MAC/address (live), Spotter `disks` (live), voice recognition/synthesis tuning (mostly restart), and write-only Hugging Face/Civitai tokens per node. Hermes `toolsets` and `search_url` also move (live for new calls) *(agent default)* |
| 8 | Restart follow-up | Show a "Waiting for restart since …" notice, plus a **restart button** for holders of `restart-services`, using the existing Components restart action |
| 9 | Settings history | **Revert one change, export as JSON, and import.** Import shows a validated preview and writes nothing until confirmed |

### Security and sign-in

| # | Topic | Decision |
|---|---|---|
| 10 | Sign-in throttle | **5 failures per 15 minutes locks for 30 minutes.** |
| 11 | Lock scope | **Per account and client address.** A failure streak locks only that address for that account, so the owner can still sign in from elsewhere. Also add a per-address counter across all accounts at 4× the threshold (20 failures), to stop password spraying *(agent default)*. Add a CLI command to list and clear locks (`weightroom user unlock NAME [--address A]`). Log `sign-in` `throttled` events. Locked attempts are refused before password hashing |
| 12 | Client address behind a proxy | *(agent default, required for 10–11)* Add a **file-tier** `trusted_proxies` list to `settings/weightroom.toml`, default empty. Only when the socket peer is in that list is `X-Forwarded-For` trusted, and then only its last untrusted hop. If sign-ins arrive only from loopback or a proxy address while `trusted_proxies` is empty, the Security page shows a warning, because per-address locks then act like account locks |
| 13 | Password minimum | **8 characters, for new or changed passwords only.** Existing passwords keep working |
| 14 | API key lifetime | Keys get an **optional expiry; the default is never.** `api_key_max_days = 0` (no limit). The API keys page offers 30/90/365 days or never. Existing keys are untouched |
| 15 | Idle sessions | `session_idle_hours`, **default 0 (off)** |

### Alerts and notifications

| # | Topic | Decision |
|---|---|---|
| 16 | Channels | **Generic webhook, ntfy and email (SMTP).** No Gotify. Webhook: JSON body with episode id, condition, source, title, state, times and a link, signed with an `X-Suite-Signature: sha256=…` HMAC header. ntfy: server URL, topic and optional token. SMTP: host, port (587 with STARTTLS by default, or 465 TLS), from, to and username. Every secret is write-only, kept in an owner-only key file under the keys folder (pattern: `voice/telephone_keys.py`) *(agent defaults for formats)* |
| 17 | Recipients | **System-wide channels plus personal opt-in.** Everyone can route their existing personal notices (security, spending, jobs; `loadcoach/preferences.py`) to their own ntfy topic or email under My account → Alerts & privacy. Holders of `manage-settings` can also route operational alerts. Personal channels are ntfy and email only; webhooks are system-wide only *(agent default)* |
| 18 | New thresholds | **On by default:** disk free below 10%, and a node unreachable for 10 minutes. **Off (0):** GPU memory and host RAM percent |
| 19 | Delivery behaviour | *(agent defaults)* Send when an episode opens or resolves. `notify_repeat_minutes = 0` (no repeats). Quiet hours are empty. Retries are bounded (3, with backoff), and failures are recorded on the episode and shown on the settings page. A "Send test" button is available per channel |

### Limits, retention and display

| # | Topic | Decision |
|---|---|---|
| 20 | Upload and content limits (§8.4) | **Global values plus per-group overrides.** Defaults are today's constants. Groups get a Limits section on the Groups page; blank means inherit the global value. Every value is capped by a fixed ceiling in code, **except for groups with `superuser`**, which are exempt. The effective limit is the group override if set, otherwise the global value, and it is checked at the point of use |
| 21 | Chat expiry | Build `chat_keep_days`, **default 0 (keep for ever).** It counts from the conversation's last activity (opened or new message). Daily sweep *(agent default)* |
| 22 | Scheduled backups | Build `backup_every` (off/daily/weekly) and a time, **default off**, run by the existing Schedules worker. The time defaults to 03:00 in the control node's time zone. Rotation follows Backups to keep |
| 23 | Display and defaults | Build **all four:** `site_name` (default "openWeightGym"), `site_banner` (text, level info/warning/danger, optional end time), default palette and appearance for accounts that never chose, and default chat model or role (empty means the first one allowed) |
| 24 | `max_queue`, `max_loaded_models` | Default 0 (no cap) *(agent default)* |

### Design, wording and process

| # | Topic | Decision |
|---|---|---|
| 25 | Design | **No proposal round.** Create **one new, professional and polished settings design** and build it live; the owner will adjust it afterwards. It covers **all settings pages**: a shared settings workspace layout with section navigation, search, a "Changed only" filter, and consistent changed/pending-restart/refused states. It is used by General, the eight new pages, and the settings parts of Compute nodes, Voice, Tools and Allowances. Extend `parts.html`, `settings_sections.html`, `setting_row.html` and `settings_program.html` rather than writing new markup. Keep all form names, IDs, HTMX targets and the no-JavaScript fallback. Use the existing CSS tokens. Record the new components in the AGENTS.md template index |
| 26 | Wording | Say **"control node"** in all new and edited user-facing text. A separate repo-wide rename is already underway in the working tree; follow it and do not redo it. Rename any leftover "front" only on pages this work touches. Config keys (`front =`, `keep_console_settings_on_front`, `toollocker_runs_on_front`) and code identifiers stay as they are |
| 27 | Uncommitted work | **Work on top of the current tree, and commit whole files** when a settings task edits a file that already has uncommitted changes, including the other changes in it. Never stage files this work did not touch, and never revert anyone else's changes. Run the full test suite before each push, because those commits will carry the other changes too. **Before pushing, run the tests in a clean worktree of the commit** (`git worktree add`), because a committed file may import an untracked one (for example, `turnstile/app.py` imports `turnstile/tool_loop.py`). Add any untracked file that a committed file needs to the same commit, and say so in the commit message. The pending Responses tool limits therefore ship with whichever commit first touches their files |
| 28 | Git | **Small commits per task, on `main`, pushed to `origin/main` after each phase** passes its tests. Commit messages end with the session's attribution line |
| 29 | Deploy | **After each phase**, run `owg update; owg restart` on the control node (`ssh` to k-srv as in the owner-deploys memory). Then verify on the live pages that each changed setting shows its new tier, and that a test change takes effect without a restart wherever it is marked live. Revert the test change afterwards |

### Decisions made during implementation

- Catalogue entries describe top-level settings; tables retain their existing whole-table POST contract. Nested tuning entries will validate the editable subset without exposing model paths.
- Phase 0 verification: the clean `717890d` full suite reports 3,708 passed, 322 failed, 11 errors and 60 skipped; all 437 settings-focused cases pass. Baseline `b040cbc` reproduces eight console and 109 legacy-service failures. Final shared `28e06ec` passes 482 focused settings/API checks. Machine-readable evidence: `/home/jpk/ai/owg-settings-phase0-full.xml`, `owg-settings-phase0-focused-results.json` and `owg-settings-phase0-live.json`; broader failures include legacy routing and control-node wording expectations.
- Live verification uses an expiring session for the existing owner account, revoked afterward. No accounts or provider/channel credentials are created.
- Live template time zones use a per-render context function to preserve existing template callers; Audio routes receive a settings getter so replaced immutable settings are visible to new requests.
- Conditional retirement uses catalogue context (`front`) so the shared compatibility map still supports standalone nodes; node admission and startup filter retired overrides without deleting them.
- Clean-worktree verification exposed the rename’s uncommitted `weightroom/display.py::node_label` dependency; include it with the shared configured-timezone fallback before deployment.
- CPU recognition endpoint limits use the existing voiced-sample evidence per stream, captured at session creation, so live endpoint changes share loaded weights and leave active sessions intact.
- No exact commit attribution line was supplied in this session; commits use `Co-authored-by: Codex <noreply@openai.com>`.

## 1. Summary

- **Tiers.** Today every setting is one of three kinds. `now` settings apply within seconds,
  `restart` settings apply at the next service start, and `file` settings are read-only on the site.
  The tier is set by `CHANGEABLE` in `loadcoach/settings.py`. Labels are in
  `weightroom/setting_labels.py`. Validation is written out separately in each service's loader
  and in `ConsoleSettings.handle`.
- **Read-only values.** 53 file values are reported: 29 on the control node, 15 per compute node and 9 for
  the voice gateway. **Ten could move to the site safely**, and **seven are retired or
  derived** and should not be shown as settings at all (§4). The rest are addresses, ports,
  paths, keys, store bindings or execution policy. They should stay in the file.
- **Restart-only settings.** **Eight restart-only settings could apply at once.** Most are
  restart-only because weightroom reads them only when it starts, not because the change needs a
  restart (§5).
- **Biggest correctness issue.** The control node's event-log retention (`[turnstile] keep_days`) is
  read-only. The per-node "Event retention" that *is* editable only trims each node's local
  copy, because the log has been forwarded to the control node since milestone 91 (§6, P1).
- **Missing settings.** The biggest gaps are sign-in throttling, outbound alert delivery
  (webhook, ntfy, email), upload and collection limits that are now hard-coded, a node-level queue
  depth cap, optional allowance enforcement, and write-only download tokens (§8).
- **Proposal.** Build one typed **setting catalogue** that drives the tiers, labels, validation and
  docs. Then add **seven new settings pages** and extend four existing ones (§10–11).

## 2. How settings work now

| Piece | Where | Notes |
|---|---|---|
| Starting values (site-changeable) | `settings/defaults/NAME.toml` | Comment above each key becomes the "Why?" text |
| Machine values (read-only on site) | `settings/NAME.toml` (from `settings/examples/`) | Laid over defaults; tables merge |
| Site overrides | `settings_changed` table (control node for the control node programs; LoadCoach DB per node) | `Changes.put/take_away`; "Revert to file" removes the row |
| What each service runs with | `settings_seen` table, posted by each service | `said()` hides names matching `key/token/secret/password` |
| Tier | `CHANGEABLE` in `loadcoach/settings.py` | Unlisted keys are `file` |
| Live apply | dataclass services: `taken()` copies `now` fields; dict services: `with_changes()`; weightroom: `in_step()` copies a hand-picked subset; turnstile: `console_settings.changes.of('turnstile').get(...)` lambdas | Each service does it differently |
| Labels/sections | `weightroom/setting_labels.py` `LABELS`, `sections()` | Unlabelled keys fall into "Additional settings" |
| Validation | `loadcoach/config.py checked()`, `turnstile/app.py load_settings`, `weightroom/app.py load_settings`, `turnstile/settings.py handle` | The same rules are written two or three times |
| Pages | `/settings` (General), `/compute-nodes`, `/system/voice`, `/system/audio`, `/settings/image`, `/tools`, `/backups`, `/users`, `/groups`, `/allowances` | Read-only table on General, Compute nodes and Voice |

## 3. Current site-changeable settings

Status as seen on the live site. **R** = restart tier. *Pending* = in the working tree but not
yet deployed (the Responses tool limits do not appear on the live General page).

| Group | Program · setting | Page |
|---|---|---|
| Platform services | weightroom `allow_image_generation`, `allow_speech`, `allow_audio_generation`, `allow_model_downloads`, `allow_classifiers` | General |
| Requests and queue | turnstile `wait` **R**, `allow_caller_to_specify_destination`; loadcoach `load_timeout`, `max_wait`, `max_estimated_wait` | General / Compute nodes |
| Tools | turnstile `response_max_tool_calls`, `response_max_tool_seconds` *(pending)*; toollocker `call_timeout`, `toolsets`, `tool_levels` | General / Tools |
| Retention | turnstile `response_keep_days`, `files_every`; weightroom `backup_retention`; loadcoach `keep_days`, `batch_keep_days`, `keep_text`, `unlogged`; spotter `keep_days`, `files_every` | General / Compute nodes / Backups |
| Monitoring and alerts | weightroom `alert_gpu_temperature`, `observation_error_percent`, `observation_p95_seconds`, `observation_min_samples`, `observation_window_seconds`, `observation_alert_hold_seconds`, `third_party_services`; turnstile/spotter `component_versions_every`; spotter `sample_every` | General / Compute nodes |
| Power and idle | spotter `sleep_after`, `busy_gpu`, `busy_processor`; loadcoach `model_idle_minutes`, `cpu_idle_minutes` | Compute nodes |
| Memory policy | loadcoach `card_margin`, `host_ram_headroom_mib`, `host_ram_limit_mib`, `disable_ram_offload` **R**, `disabled_gpus`, `engine_gpus` | Compute nodes |
| Runtimes | loadcoach `engines` **R**, `llama_server` **R**, `model_defaults` | Compute nodes |
| Model files | loadcoach `models_folder` **R**, `models_storage_folder` **R**; turnstile `models_folder`, `models_storage_folder`, `artifact_machine` **R** | General (location card) / Compute nodes |
| Display and costs | weightroom `timezone` **R**, `mock_input_rate` **R**, `mock_output_rate` **R** | General |
| Media | weightroom `audio_*_model` ×5 **R** | Audio |
| | turnstile `image_adult_previews` | Image |
| | turnstile `preferred_voice_node` | General |
| Voice (compute) | voice `max_active_sessions`, `max_starting_sessions`, `max_session_minutes`, `idle_seconds`, `hold_seconds`, `hold_every`, `hold_after_last_call`, `speech_threshold`, `profiles`, `greetings`, `model_parallel` **R** | Voice |
| Voice gateway | `modes`, `profile`, `soundroom_profile`, `startup_seconds`, `max_active_sessions`, `max_starting_sessions`, `hermes_*` ×6, `telephone`, `public_port` **R**, `public_host` **R** | Voice / Telephone |

Groups, users, roles, cloud accounts, allowances, schedules and models are records kept in
databases, not program settings. Their own pages manage them, and they are outside this review
except where §8 adds policy around them.

## 4. Read-only machine values: verdict for each

**Move to site** means the value carries no execution or credential risk, is safe to get wrong
(it can be reverted, and the file value is the fallback), and is something an operator changes
in normal use.

### Control node: weightroom / turnstile / toollocker

| Value | Verdict | Tier if moved | Reason |
|---|---|---|---|
| turnstile `keep_days` | **Move to site** | now | This is the suite's event-log retention since milestone 91. The editable node `keep_days` is not (P1) |
| weightroom `observation_keep_days`, `observation_success_percent`, `observation_max_spans`, `observation_quiet_routes` | **Move to site** | now | The policy is a DB row (`observation_policy`) that every writer and pruner reads. `in_step` only has to call `set_policy` |
| turnstile `busy_retry_seconds` *(not set in file today)* | **Move to site** | now | Only builds a Retry-After header |
| turnstile `machines_poll_seconds` *(not set)* | **Move to site** | now | Read per poll. Bound it to 2–300 s |
| turnstile `max_in_flight` *(not set)* | **Move to site** | restart | Sizes the httpx pool when turnstile starts |
| per-node `[machines.NAME].wake_mac`, `wake_address` | **Move to site** (Compute nodes) | now | Used by the user-facing Wake feature. Only a MAC address and a broadcast address |
| weightroom `wake_mac`, `wake_address` (top level) | **Hide as retired** | — | Superseded by per-node wake settings. Show only if set, with a note to move them |
| weightroom `compute`, `spotter` | **Hide as retired** | — | The loader already treats them as retired |
| weightroom `door` | **Hide as derived** | — | Computed from `[turnstile].host`, not a file key |
| turnstile `keep_console_settings_on_front` | **Hide as deprecated** | — | Ignored, and the loader prints that it is ignored |
| weightroom `host`, `port`, `data`, `databases`, `key_file`, `plugins_dir`, `chat_model_url`, `toollocker`, `voice_gateway`; turnstile `host`, `ports`, `voice_port`, `toollocker_url`, `machine_key_file`, `hosted_keys`, `[machines.NAME].compute/spotter/machine_key_file/formerly` | Keep in file | restart | Listeners, upstreams, store bindings and keys. A wrong value can lock the site out or point it at another host. Pairing already manages `[machines]` |
| weightroom `secure` | Keep in file | restart | Setting it to true on plain HTTP signs everyone out with no way back in from the browser |
| toollocker `host`, `port`, `front` | Keep in file | restart | Listener and authority binding |
| toollocker `servers` (command servers) | Keep in file | restart | Arbitrary program execution |
| toollocker `servers` (remote `url` servers) | **Move to site** (Tools), superuser only | now | The door already admits new servers within 30 s. Keys use the write-only credential pattern from `voice_admin_parts.html`. Owner decision D4 |

### Each compute node: loadcoach / spotter / voice

| Value | Verdict | Tier if moved | Reason |
|---|---|---|---|
| spotter `disks` | **Move to site** (Compute nodes) | now | `settings.disks` is read on every reading. Paths are only stat'ed, never written |
| loadcoach `hub_token_file`, `civitai_token_file` | **Keep the path in the file; set the token from the site** (write-only) | now | Tokens are read each time they are needed. Writing them atomically follows `voice/telephone_keys.py` |
| voice `[recognition]` `threads`, `decoding_method`, `max_active_paths`, `trailing_silence_ms`, `max_utterance_seconds` | **Move to site** (Voice) | restart (`trailing_silence_ms`, `max_utterance_seconds`: now, for new sessions) | Tuning values. The model paths stay in the file |
| voice `[synthesis]` `speaker`, `threads`, `workers` | **Move to site** (Voice) | restart | Same as above |
| loadcoach `host`, `port`, `data`, `databases`, `front`, `machine_key_file`, `machine_name`, `places`, `nvidia_smi`, `toollocker_runs_on_front` | Keep in file | restart | Identity, trust, listeners, executables |
| spotter `host`, `port`, `data`, `databases`, `loadcoach`, `turnstile_notice` | Keep in file | restart | Listener, bindings, signed notice URL |
| spotter `services`, `system_services` | Keep in file | restart | This allowlist bounds which units the site may restart. It has to stay outside the site's reach |
| voice `host`, `port`, `compute`, `key_file`, `machine_key_file`, `data`, `observability_*`, all model paths | Keep in file | restart | Listener, trust, paths |

### Voice gateway (control node)

| Value | Verdict | Tier if moved | Reason |
|---|---|---|---|
| `[hermes].toolsets`, `search_url` | **Move to site** (Voice) | now (new calls) | Agent choices, alongside the `hermes_*` values already on the site |
| `host`, `port`, `turnstile`, `voice_turnstile`, `voice`, `key_file`, `browser_key_file`, `data`, `conversations_url`, `desktop_host`, `desktop_port`, `observability_*`, `context_key_file`, `[hermes].python/repository/provider_url/provider_key_file`, `[twilio]` paths | Keep in file | restart | Listeners, keys, executables. The Telephone page already manages Twilio |
| `origins` | Keep in file | restart | Anyone who opens the browser voice page acts as the browser account. The origin allowlist is what protects it |

## 5. Restart-only settings that could apply at once

| Setting | Why it is restart today | What makes it live |
|---|---|---|
| weightroom `timezone` | `ZoneInfo` is captured in Jinja globals at startup (`weightroom/app.py:619`) | Make the template global a function that reads the current settings. Validate with `ZoneInfo` in `ConsoleSettings` |
| weightroom `mock_input_rate`, `mock_output_rate` | Not copied in `in_step` | Copy them in `in_step` with the loader's 0–1,000,000 check |
| weightroom `audio_*_model` ×5 | Not copied in `in_step` | Copy them in `in_step`. The Audio page already reads `settings` |
| turnstile `wait` | Read as `settings.wait` per request (`turnstile/app.py:679, 959`) | Use the same `changes.of('turnstile')` getter as the other live turnstile values |
| voice `model_parallel` | Sizes per-model concurrency at start | Leave it restart-only. Changing it while sessions are open needs a drain |
| loadcoach `disable_ram_offload` | An environment variable for a model process when it starts | Keep it restart-only but relabel it "applies to the next model load" if only spawned processes read it. Check `loadcoach/runner.py` |
| loadcoach `models_folder`, `models_storage_folder`, `engines` | Startup scan and runtime discovery | Leave them restart-only. The General card already explains this |
| turnstile `artifact_machine` | Wires the file-owner client at start | Leave it restart-only |

## 6. Problems found

- **P1. Event retention is in the wrong place.** On a node with `front` set, LoadCoach forwards
  its log (`Log(..., chained=not config.front)`). `loadcoach keep_days` then trims only that
  node's local copy, which `SENT_KEPT` already limits to a day after the control node has it. The
  retention that matters is `[turnstile] keep_days`, and it is read-only. *Fix:* make turnstile
  `keep_days` live on Retention and privacy, and retire the node setting on nodes with `front`
  set (§0, decision 1).
- **P2. Observation span policy is file-only** even though its comment says "Applied when
  weightroom starts". The keys are not in `CHANGEABLE`, so the site shows them read-only.
- **P3. Validation is repeated** in up to four places per setting (loader, `checked()`,
  `ConsoleSettings.handle` if-chain, page JavaScript). Ranges already disagree. For example,
  `keep_days` is unbounded in one place and `response_keep_days` is capped at 3650 in another.
- **P4. Live-apply is written four different ways**, and weightroom's `in_step` copies each value
  by hand. Every new `now` setting needs hand-written code, and if that code is forgotten, a value
  marked live is not actually live.
- **P5. The read-only table mixes in retired and derived keys** (`compute`, `spotter`, `door`,
  `keep_console_settings_on_front`, top-level `wake_*`). It does not say which file to edit or
  how to apply a change.
- **P6. No search, no filter for changed settings, no history and no export.** The latest
  change shows who made it and when, and earlier changes are only in the event log (`settings`
  `changed`). There is no way to export or import an override set, or to compare it between nodes.
- **P7. "Needs restart" has no follow-up.** A badge appears, but the page does not say whether
  a restart is pending since the change, and it offers no restart even to holders of
  `restart-services`.
- **P8. Product limits are hard-coded** (§8.4). Raising a limit, such as five collections per
  account, needs a code change.

## 7. Comparison with similar software

| Product | Settings it exposes that openWeightGym lacks | How it applies them |
|---|---|---|
| Open WebUI | Admin Panel tabs: General, Connections, Models, Documents (chunk size, overlap, top-k, embedding model), Web search, Audio, Images, Interface (banners, default models, task/title model), Evaluations, Database export | Admin settings persist in the DB and override environment variables |
| LiteLLM proxy | Global and per-key budgets with durations, RPM/TPM limits, key expiry, Slack and webhook alerting with an `alerting_threshold`, request timeouts and retries, fallbacks | `config.yaml` (`general_settings`, `litellm_settings`, `router_settings`) plus DB-backed keys |
| LibreChat | `rateLimits` (uploads, imports, sign-in/registration windows), `fileConfig` (size, count and type per endpoint), `balance` (starting credits, auto-refill), registration and allowed domains | `librechat.yaml` + environment variables. Restart |
| LocalAI | A runtime settings page: watchdog idle/busy timeouts, max active backends, parallel requests, memory reclaimer | Web UI writes `runtime_settings.json`. Applies immediately |
| Ollama | `KEEP_ALIVE`, `MAX_LOADED_MODELS`, `NUM_PARALLEL`, `MAX_QUEUE` (503 beyond it), `KV_CACHE_TYPE`, `ORIGINS`, default context length | Environment variables. Restart |
| GPUStack | Per-worker system-reserved RAM/VRAM, scheduler placement (spread/binpack), worker labels for placement | Worker config/CLI |

Coverage is already strong for idle unloading (`model_idle_minutes`, `cpu_idle_minutes`),
memory headroom (`card_margin`, `host_ram_*`), KV cache flags (`model_defaults`), per-group
parallel and priority limits, and power management. The gaps are mainly at the platform edge:
abuse limits, notifications, enforcement and content limits.

## 8. Missing settings (proposed)

*Live* means it applies within seconds. Defaults keep today's behaviour unless noted.

### 8.1 Security and sign-in (`/settings/security`)

| Setting | Program | Default | Tier | Code |
|---|---|---|---|---|
| `sign_in_max_failures` (per account and client address, per window; 4× across accounts per address) | turnstile | 5 | live | `loadcoach/users.py` sign-in path. New failure counter in the accounts store |
| `sign_in_window_minutes`, `sign_in_lockout_minutes` | turnstile | 15 / 30 | live | Same |
| `trusted_proxies` | weightroom | [] | **file** | Needed for real client addresses behind the HTTPS proxy (§0, decision 12) |
| `password_min_length` | turnstile | 8 (applies to new or changed passwords only) | live | `weightroom user` and the Users page |
| `api_key_max_days` (0 = keys never expire, today's behaviour) and default lifetime offered on the API keys page | turnstile | 0 | live | `new_key()` and `/account/keys` |
| `session_idle_hours` (0 = off) alongside per-group `session_hours` | turnstile | 0 | live | Session check |
| `unlogged` (move here from Logs: it is a security policy) | loadcoach (per node) | false | live | Change the label only |

### 8.2 Requests, queue and routing (`/settings/requests`)

| Setting | Program | Default | Tier | Code |
|---|---|---|---|---|
| `max_queue` per node (requests waiting; above it, 503 with Retry-After, like Ollama `MAX_QUEUE`) | loadcoach | 0 (no cap) | live | LoadCoach line admission |
| `max_loaded_models` per node (optional cap besides memory fit) | loadcoach | 0 | live | Placement/eviction |
| `role_tries`, `role_loaded_bonus`, `role_fast_loaded_bonus`, `role_unknown_speed`, `role_unknown_load` (now `TRIES`, `LOADED_BONUS`, … in `turnstile/roles.py`, `loadcoach/roles.py`) | turnstile/loadcoach | today's constants | live | Advanced section |
| `batch_max_lines`, `batch_max_mib`, `batch_tries`, `batch_retry_seconds` (now `MOST_LINES`, `MOST_BYTES`, `TRIES`, `AGAIN_AFTER`) | loadcoach | 10000 / 50 / 3 / 30 | live | `loadcoach/batches.py` |
| `busy_retry_seconds`, `machines_poll_seconds`, `max_in_flight` **R**, `wait` (moved from §4/§5) | turnstile | as file | live (R) | `turnstile/app.py` |

### 8.3 Alerts and notifications (`/settings/alerts`)

Alerts are in-app only today (`weightroom/alerts.py`).

| Setting | Default | Tier | Notes |
|---|---|---|---|
| `notify_webhook_url` + write-only signing secret | "" | live | JSON POST per opened/resolved episode, HMAC header |
| `notify_ntfy_url` / topic + write-only token | "" | live | Covers phones without email |
| `notify_smtp_*` (host, port, from, to) + write-only password | "" | live | Optional email |
| `notify_min_state` (opened / unacknowledged after N min) | opened | live | |
| `notify_repeat_minutes`, `notify_quiet_hours` | 0 / "" | live | |
| "Send test notification" button | — | — | Logged as a `settings` event |
| Personal ntfy/email channels (My account → Alerts & privacy) | off | live | Personal notices for everyone; operational alerts for `manage-settings` |
| `alert_disk_free_percent` | 10 (on) | live | Spotter already reports `disks` |
| `alert_node_offline_minutes` | 10 (on) | live | Turnstile machine facts |
| `alert_gpu_memory_percent`, `alert_host_ram_percent` | 0 (off) | live | Spotter readings |
| `alert_allowance_percent` (like LiteLLM `alerting_threshold`) | 0 (off) | live | `turnstile/allowances.py` |

### 8.4 Uploads and content limits (`/settings/limits`)

All of these are hard-coded constants today. They are read per request, so all are live.
Each has a global value and an optional per-group override (Groups page, blank = inherit). Both
are capped by a hard ceiling in the catalogue, except for groups with `superuser` (§0, decision 20).

| Setting | Now hard-coded at | Code |
|---|---|---|
| `chat_max_files`, `chat_max_file_mib`, `chat_max_total_mib`, `chat_max_text_kib` | 4 / 5 / 10 / 200 | `weightroom/chat_store.py` |
| `document_max_mib`, `document_max_pages` | 1 / 40 | `weightroom/document_text.py` |
| `collections_per_account`, `documents_per_collection`, `chunks_per_collection`, `chunks_per_source` | 5 / 20 / 500 / 256 | `weightroom/knowledge_store.py`, `weightroom/knowledge.py` |
| `chunk_bytes`, `chunk_overlap_bytes` (Open WebUI exposes both) | 1200 / 200 | `weightroom/knowledge.py chunks()` |
| `extraction_max_mib` | 5 | `weightroom/extraction.py` |
| `audio_upload_mib` | 32 | `soundroom/store.py` |
| `image_max_megapixels` | 32 | `mirrorwall/store.py` |
| `responses_max_records`, `responses_max_mib` | 10000 / 256 | `weightroom/response_store.py` |

### 8.5 Spending (`/allowances`, policy section)

| Setting | Default | Tier | Notes |
|---|---|---|---|
| `allowance_mode`: advisory / warn / block | advisory | live | Allowances are "never admission rules" today, so block mode is a policy change (owner decision D2). Block applies only to hosted requests, which have a recorded cost |
| `allowance_period`: rolling 24 h / calendar month | as today | live | |

### 8.6 Downloads and credentials (`/settings/downloads`)

| Setting | Tier | Notes |
|---|---|---|
| Hugging Face token, Civitai token per node (write-only, "set / replace / remove", never shown) | live | Writes the file named by `hub_token_file` / `civitai_token_file` (creating `keys/huggingface.token` if unset) with mode 600, atomically. Reuse the write-only forms in `voice_admin_parts.html` |
| `downloads_at_once` per node | live | Check `loadcoach/hub.py` for the current behaviour |
| `allow_model_downloads` | — | Stays in Platform services. Linked from here |

### 8.7 Retention and privacy (`/settings/retention`)

These gather what is scattered today, and add two settings:

| Setting | Program | Tier |
|---|---|---|
| Event log days (`[turnstile] keep_days`, P1) | turnstile | live |
| Stored Responses days | turnstile | live (exists) |
| Observation spans: days, success %, cap, quiet routes (P2) | weightroom | live |
| Request text kept, batch file days (per node) | loadcoach | live (exists) |
| Telemetry days (per node) | spotter | live (exists) |
| **New** `chat_keep_days` (0 = keep for ever; deletes conversations not opened for N days) | weightroom | live |
| **New** `backup_every` (off / daily / weekly) + time. Runs through the existing Schedules worker | weightroom | live |
| Backups to keep | weightroom | live (exists) |

### 8.8 Display and defaults (General → Display)

| Setting | Default | Tier | Notes |
|---|---|---|---|
| `site_name` | "openWeightGym" | live | Page titles and header |
| `site_banner` (text + level, optional end time) | "" | live | Like Open WebUI banners; good for maintenance notices |
| `default_palette`, `default_appearance` for accounts that never chose | current | live | Account preferences keep winning |
| `default_chat_model` (model or role) | "" (first allowed) | live | Mirrors `audio_*_model` |
| `timezone`, `mock_*_rate` (§5) | as file | live | |

## 9. Final classification by logical group

**Site · live** = editable, applies within seconds. **Site · restart** = editable, applies at
next start (page shows "Needs restart" and offers a restart). **File · restart** = edit
`settings/NAME.toml` and restart the service.

| Group | Site · live | Site · restart | File · restart |
|---|---|---|---|
| **Platform services** | `allow_*` ×5 | — | — |
| **Requests and queue** | `allow_caller_to_specify_destination`, `wait`, `busy_retry_seconds`, `machines_poll_seconds`, node `load_timeout`, `max_wait`, `max_estimated_wait`, *new* `max_queue`, `max_loaded_models`, routing tunables, batch limits | `max_in_flight` | turnstile `host`, `ports`, `voice_port` |
| **Tools** | `response_max_tool_calls`, `response_max_tool_seconds`, `call_timeout`, `toolsets`, `tool_levels`, *new* remote MCP servers (D4) | — | `toollocker host/port/front`, command `servers`, `toollocker_url` |
| **Retention and privacy** | event log days, Responses days, observation span policy, `keep_text`, `batch_keep_days`, telemetry days, `backup_retention`, *new* `chat_keep_days`, `backup_every` | — | `data`, `databases` |
| **Security and sign-in** | *new* throttling, password length, key lifetime, idle sessions; `unlogged`; per-group `session_hours`, `second_step` (Groups) | — | `secure`, `origins`, `key_file`s, `machine_key_file`s, `hosted_keys` |
| **Alerts and notifications** | GPU temperature, observation thresholds, `third_party_services`, `component_versions_every`, *new* channels and thresholds | — | spotter `services`, `system_services` |
| **Power and idle** | `sleep_after`, `busy_gpu`, `busy_processor`, `model_idle_minutes`, `cpu_idle_minutes`, *new* per-node `wake_mac`, `wake_address` | — | spotter `turnstile_notice` |
| **Telemetry** | `sample_every`, `files_every`, *new* spotter `disks` | — | spotter `host/port/data/databases` |
| **GPU and memory** | `card_margin`, `host_ram_headroom_mib`, `host_ram_limit_mib`, `disabled_gpus`, `engine_gpus`, `model_defaults` | `disable_ram_offload` | `nvidia_smi`, legacy `gpus` |
| **Runtimes** | — | `engines`, `llama_server` | — |
| **Model files and downloads** | control node `models_folder`, `models_storage_folder`, *new* tokens (write-only), `downloads_at_once` | node `models_folder`, `models_storage_folder`, `artifact_machine` | `places`, `hub_token_file`/`civitai_token_file` paths |
| **Uploads and limits** | *new* §8.4 (all) | — | — |
| **Spending** | allowances (records), *new* `allowance_mode`, `allowance_period`, `alert_allowance_percent` | — | — |
| **Display and defaults** | `timezone`, `mock_*_rate`, `audio_*_model`, *new* `site_name`, `site_banner`, default palette and chat model | — | `host`, `port`, `chat_model_url`, `plugins_dir` |
| **Images** | `image_adult_previews` | — | — |
| **Voice** | session limits, `speech_threshold`, `profiles`, `greetings`, gateway `modes`, `profile`, `soundroom_profile`, `startup_seconds`, `hermes_*`, `telephone`, `preferred_voice_node`, *new* `[hermes].toolsets/search_url`, `trailing_silence_ms`, `max_utterance_seconds` | `model_parallel`, `public_port`, `public_host`, *new* recognition/synthesis `threads`, `workers`, `decoding_method`, `max_active_paths`, `speaker` | model paths, listeners, keys, `[hermes]` executables |
| **Identity and topology** | — | — | `machine_name`, `front`, `[machines.NAME]` addresses and keys (pairing), all listeners and store bindings |

## 10. Settings pages

### Existing pages and their changes

| Page | Change |
|---|---|
| **General** `/settings` | Keep Platform services, Model file locations, Display and defaults (adds §8.8), Speech. Move Requests, Retention and Alerts to their own pages, leaving summary links. Replace the read-only table with a link to Machine configuration |
| **Compute nodes** `/compute-nodes` | Add Wake (per-node `wake_mac`/`wake_address`), Telemetry `disks`, `max_queue`, `max_loaded_models`. Relabel the node `keep_days` (P1). Replace the per-node read-only table with a link to Machine configuration for that node |
| **Tools** `/tools` | Add a "Remote MCP servers" editor (URL, description, level, write-only key or public) if D4 is approved. Command servers stay read-only with their file path shown |
| **Voice** `/system/voice` | Add Recognition and Synthesis tuning (restart) and Hermes tools (live) |
| **Spending allowances** `/allowances` | Add the policy section (§8.5) |

### New pages (under Settings, `manage-settings`)

| # | Page | Route | Nav group | Contents |
|---|---|---|---|---|
| 1 | Requests and queue | `/settings/requests` | Platform | §8.2 plus the current Requests settings. Control node values and fleet defaults, with per-node overrides linked to Compute nodes |
| 2 | Retention and privacy | `/settings/retention` | Platform | §8.7. One table of everything kept, how long it is kept, and its current size where known |
| 3 | Alerts and notifications | `/settings/alerts` | Platform (or System → Monitoring beside `/alerts`) | §8.3 thresholds, channels, test send |
| 4 | Security and sign-in | `/settings/security` | Access & spending | §8.1, with read-only cookie and origin status (`secure`, `origins`) and where to change them |
| 5 | Uploads and limits | `/settings/limits` | Platform | §8.4, each with its hard ceiling shown |
| 6 | Downloads and credentials | `/settings/downloads` | Models (or Settings → Platform) | §8.6, per node, write-only tokens |
| 7 | Machine configuration | `/settings/machine` | Platform | Node selector. Every file value with the file it comes from (`settings/NAME.toml` vs `defaults/NAME.toml`), whether it differs from the default, retired/derived/deprecated flags, and the restart command for that service. Read-only |
| 8 | Settings history | `/settings/history` | Platform | `settings` events from the event log: who, when, old → new, program/node. Revert a single change. Export/import of the override set as JSON (import shows a preview and validates every value before anything is written) |

These follow the AGENTS.md process. First check `parts.html`, `settings_sections.html`,
`setting_row.html` and `settings_program.html`, and extend them. Pages 1–5 are
`settings_sections.html` filtered by catalogue `page`. Add each new route to `NAV_SECTIONS`
and to the template index. Alerts and notifications goes under Settings → Platform. There is
no design-proposal round: build one new settings design across all settings pages, live
(§0, decision 25).

## 11. Work plan

### Status

- [x] 0.1 Catalogue entries and typed contracts (221 catalogue tests pass).
- [x] 0.2 Derive tiers, labels and section order (235 focused tests pass; broader console failures under baseline review).
- [x] 0.3 Shared validation in APIs and loaders (323 focused checks pass; secret reports are filtered at storage).
- [x] 0.4 Common live getter and generic live application (374 focused checks pass; resets restore original file/default values).
- [x] 0.5 Catalogue, validation, live and secret tests (437 focused checks pass).
- [x] Phase 0 full clean-commit run, shared main pushed, normal deployment and live verification (28e06ec; five settings surfaces; Backups 10 → 11 → 10 without restart).
- [x] 1a Event retention and conditional node retirement (416 focused checks pass).
- [x] 1b Live observation policy (414 focused checks pass; shared policy updates on save).
- [x] 1c Live display, costs and audio defaults (459 feature checks and all 39 UI consistency/browser cases pass).
- [x] 1d Live control node request settings (447 focused checks pass; connection pool size stays restart-only).
- [x] 1e Live disk monitoring paths (427 focused checks pass; cached readings invalidate on selection changes).
- [x] 1f Per-node Wake overrides (445 focused checks pass; offline edits preserve node files, listeners and keys).
- [x] 1g Voice recognition and synthesis tuning (498 checks pass; nested overrides preserve paths and freeze restart fields).
- [ ] 1h Hermes tools for new calls.
- [ ] 1i Retired, derived and deprecated machine values.
- [ ] 1j Responses limits dependencies; Phase 1 deployed and verified.
- [ ] 2.1 Shared design, grouped routes and navigation.
- [ ] 2.2 Search and Changed only on every settings surface.
- [ ] 2.3 Pending restart status and authorized restart action.
- [ ] 2.4 Machine configuration page.
- [ ] 2.5 History, revert and validated export/import.
- [ ] 2.6 UI checks and documentation; Phase 2 deployed and verified.
- [ ] 3.1 Security and sign-in.
- [ ] 3.2 Requests, admission, placement, routing and batches.
- [ ] 3.3 Notifications, personal channels and thresholds.
- [ ] 3.4 Global limits and per-group overrides.
- [ ] 3.5 Spending policy.
- [ ] 3.6 Download credentials and concurrency.
- [ ] 3.7 Chat retention and scheduled backups.
- [ ] 3.8 Display and defaults.
- [ ] 3.9 Remote MCP servers; Phase 3 deployed and verified.
- [ ] 4 Documentation, complete clean-worktree suite, final deployment and live verification.

### Phase 0: Setting catalogue (foundation; fixes P3 and P4)

1. Add `loadcoach/setting_catalog.py` with one entry per setting:
   `program, name, kind (bool/int/float/text/list/table/choice/secret), tier (now/restart/file),
   scope (front/node), page, section, label, unit, min, max, choices or choices_from, help, retired/derived flag, check()`.
   Every key in `settings/defaults/*.toml` and `settings/examples/*.toml` gets an entry,
   including file-tier ones (for the Machine configuration page).
2. Derive `CHANGEABLE` from the catalogue (keep the name and shape for compatibility).
   Derive `setting_labels.LABELS` and `sections()` order from it. Unknown keys still fall to
   "Additional settings".
3. Add one `validate(program, name, value)` used by `ConsoleSettings.handle`, by LoadCoach's
   settings POST, and by each loader (`checked()`, turnstile/weightroom `load_settings`). Remove
   the if-chains in `turnstile/settings.py`.
4. Add one live getter, `live(program, name, fallback)`:
   - control node services read `console_settings.changes.of(program)`;
   - weightroom's `in_step` copies every catalogue `now` field generically (validated) instead
     of the hand-written list;
   - node services keep `taken()` / `with_changes()`, which already work from `CHANGEABLE`.
5. Tests (`tests/test_setting_catalog.py`):
   - every defaults and example key is catalogued;
   - every `now` setting changes behaviour without a restart (one parametrised test per service using its getter);
   - validation gives the same result through the loader and through the site;
   - secret-kind values are never in `settings_seen`.

### Phase 1: Make existing values live or editable (quick wins)

| Task | Files |
|---|---|
| 1a. turnstile `keep_days` → live. Control node `Log.keep_days` read through the getter on each prune. Retire the node `keep_days` on nodes with `front` (§0, decision 1) | `turnstile/app.py`, `turnstile/events.py`, `setting_labels` via catalogue |
| 1b. Observation span policy → live. `in_step` calls `set_policy` when the values change. Site validation uses `check_policy`. Update the defaults comment | `weightroom/app.py`, `turnstile/settings.py`, `settings/defaults/weightroom.toml` |
| 1c. `timezone`, `mock_*_rate`, `audio_*_model` → live | `weightroom/app.py` (Jinja global function, `in_step`) |
| 1d. turnstile `wait`, `busy_retry_seconds`, `machines_poll_seconds` → live. `max_in_flight` → site · restart | `turnstile/app.py` |
| 1e. spotter `disks` → live, validated as absolute paths, at most 32 | `spotter/app.py`, catalogue |
| 1f. Per-node wake settings → control-node-owned override in `settings_changed` under `machines:NAME`, laid over `[machines.NAME]` on read | `turnstile/machines.py`, `turnstile/nodes.py`, `compute_node_parts.html` |
| 1g. Voice recognition/synthesis tuning → `voice` catalogue entries (nested keys, like `profiles`) | `voice/service.py`, `laid_over` table merge for `recognition`/`synthesis` |
| 1h. Hermes `toolsets`, `search_url` → live for new calls | `voice/gateway.py`, `voice/hermes.py` |
| 1i. Hide retired/derived keys (P5). Mark deprecated ones | `settings_machine_values.html`, catalogue flags |
| 1j. The pending Responses tool limits ship with whichever commit first touches their files (§0, decision 27) | — |

### Phase 2: Settings page infrastructure (P6, P7)

1. Pages 1–5: routes in `weightroom/app.py` that render `settings_sections.html` with
   catalogue-filtered groups. Add nav entries in `navigation.py` `NAV_SECTIONS` and the link list.
2. Search box and "Changed only" filter on every settings page. This is client-side in
   `settings.js` and must not break the no-JavaScript fallback.
3. Restart follow-up. For each `restart` setting, compare the change time with the service's
   last `settings_seen` start time, and show "Waiting for restart since …". Offer a restart
   button, using the existing Components restart action, to holders of `restart-services`.
4. Page 7, Machine configuration: catalogue + `settings_seen` `file` values, source file, differs-from-default.
5. Page 8, Settings history: query the `settings` events. Add `old` to the logged detail in
   `ConsoleSettings` and LoadCoach. Single-change revert and JSON export/import, both through the
   same validated POST.
6. Update the AGENTS.md template index and docs/README.md. Run `tests/test_ui_consistency.py`
   and the all-page browser checks, including narrow layouts and empty/error states.

### Phase 3: New settings, by group (each a separate commit with tests)

| # | Group | Main work |
|---|---|---|
| 3.1 | Security | Failure counter table in the accounts store, keyed by account and client address. Checked in sign-in before the password hash, so throttled attempts cost no hashing. Lockout response is 429 with Retry-After. Log `sign-in` `throttled`. Password length enforced in user add/change and the Users page. Key lifetime in `new_key()` and the API keys page. Idle sessions use a last-seen time per session key |
| 3.2 | Requests | `max_queue` and `max_loaded_models` in LoadCoach admission and placement. Routing and batch constants become catalogue reads with today's values as defaults |
| 3.3 | Alerts | New `weightroom/notify.py`: webhook (HMAC), ntfy and SMTP senders, run from the existing alert reconciliation when an episode opens or resolves. Retries are bounded, failures are recorded on the episode, and there is a test-send endpoint. Write-only secrets live in owner-only key files, following `telephone_keys.py`. New thresholds use readings Spotter and turnstile already collect |
| 3.4 | Limits | Replace each §8.4 constant with a catalogue read at the point of use, with ceilings. Show the effective limits in the relevant page hints (chat attachments, Collections) |
| 3.5 | Spending | `allowance_mode` and `allowance_period` in `turnstile/allowances.py`. Block mode applies to hosted requests only and returns a clear error naming the allowance. Gated on D2 |
| 3.6 | Downloads | Write-only token forms per node. Node endpoint writes the token file atomically with mode 600. `downloads_at_once` in `loadcoach/hub.py` |
| 3.7 | Retention | `chat_keep_days` daily sweep in `chat_store.py`. Backup schedule as a built-in Schedules function |
| 3.8 | Display | `site_name`, `site_banner` in `frame.html` through the existing `alert` macro. Default palette and chat model read where account preferences fall back |
| 3.9 | Tools | Remote MCP server editor stored on the control node. The door merges them with the file's servers, and file entries win on a name clash. Gated on D4 |

### Phase 4: Documentation, tests and deployment

- Update every changed comment in `settings/defaults/*.toml` and `settings/examples/*.toml`.
  The comments are the "Why?" text on the site.
- Update `docs/operations.md` (which file holds what, the restart matrix) and `app-walkthrough.md`.
- After each phase: commit, push to `origin/main`, then deploy with `owg update; owg restart`.
  Check on the live Settings pages that each moved setting shows its new tier, and that a test
  change takes effect without a restart where it is marked live (§0, decisions 28–29).

## 12. Owner decisions

All resolved in §0. D1 = retire on nodes with `front`. D2 = warn plus optional block for hosted
requests. D3 = 5 failures per 15 minutes, 30-minute lock, per address, 8-character minimum for new
passwords. D4 = yes, superusers only. D5 = Settings → Platform.

## Sources

- Open WebUI settings: <https://docs.openwebui.com/getting-started/settings>, <https://docs.openwebui.com/features/administration/>
- LiteLLM proxy configuration and budgets: <https://docs.litellm.ai/docs/proxy/config_settings>, <https://docs.litellm.ai/docs/proxy/users>
- LibreChat configuration: <https://librechat.ai/docs/configuration/librechat_yaml/example>
- LocalAI runtime settings and memory management: <https://localai.io/features/runtime-settings>, <https://localai.io/docs/advanced/vram-management/>
- Ollama server configuration: <https://docs.ollama.com/faq>
- GPUStack (from a mirror of its CLI reference): <https://cdn.jsdelivr.net/gh/gpustack/gpustack@main/README.md>
