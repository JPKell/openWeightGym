# Application sweep — 2026-10-20

This report records the sweep and subsequent Requests changes, validation and deployment across Suite.

## Changes by code section

- **Accounts — `loadcoach/users.py`, `loadcoach/statement.py`:** recheck identity and credential state after asynchronous password verification; preserve lockout across overlapping failures; create private, unique, flushed key-rotation files before atomic replacement.
- **Storage — `db/connection.py`:** escape literal percentages only when binding PostgreSQL parameters; roll back failed commits so shared connections remain usable.
- **Model files — `loadcoach/files.py`, `loadcoach/app.py`, `loadcoach/hub.py`, `turnstile/app.py`:** give cross-drive copies unique temporary files and refuse competing destinations; reject colliding download filenames; correct reservations for oversized partial files and stop oversized responses; restrict cleanup routes so they cannot intercept copy/move/delete requests.
- **Tools — `toollocker/servers.py`, `toollocker/door.py`:** join old readers/children before restart, clean up failed startup and sends, serialize discovery, close HTTP clients at shutdown, correlate replies and bound responses/catalogues to 16 MiB. Discovery rejects duplicate names, invalid entries and repeated/excessive cursors (128 continuation pages; 4,096 tools).
- **Tool hosts — `toollocker/tools/`:** reject empty keys and invalid HTTP body lengths, bound socket reads to 30 seconds, and bound text excerpts to 2,000 lines/262,144 characters with a starting-line ceiling of 1,000,000.
- **Durable work — `turnstile/tasks.py`, `loadcoach/schedule_time.py`:** repair evidence-queue retries; reject malformed calendar fields, unknown timezones and invalid occurrence timestamps without server errors.
- **Admission and shutdown — `loadcoach/runner.py`:** bound optional final memory sampling before shielded child cleanup, so stalled telemetry cannot indefinitely retain a model process; replace mutable helper defaults with immutable tuples.
- **Console and chat — `weightroom/app.py`, `weightroom/conversations.py`, `weightroom/pages/chat_main.html`:** bound streamed form/account/chat input, reject invalid form encoding or excessive fields, prevent private HTML caching and stack mobile chat controls. General forms allow 256 KiB, benchmark forms 6 MiB, chat forms/API input 24 MiB and account API input 64 KiB; forms allow 4,096 fields.
- **Source documentation — maintained Python and browser code:** add 1,060 definition docstrings and 130 JSDoc summaries; retain vendor/test-fixture exclusions. Admin Docs reads Python ASTs and attached JSDoc to show definitions, signatures and line numbers. It never imports/executes sources, escapes displayed text, parses off the event loop and caches up to 256 file revisions using modification time and size.
- **Docs layout — `weightroom/pages/docs_*.html`:** replace empty outline cards with a compact scope summary; correct the system map's control-node-owned ToolLocker, external providers and shared stores.
- **Readability — workflow storage/orchestration and form restoration:** expand dense statements into conventional Python blocks.
- **Verification — `tests/`, `pyproject.toml`:** enforce production docstring coverage, test failure/concurrency boundaries, update obsolete RSS/protocol/settings assertions and bound telemetry streams in browser fixtures; run tests against this checkout independently of stale editable-install metadata.
- **Dependencies — `pyproject.toml`, IFEval, installation/deployment scripts:** replace NLTK 3.9.2 with 3.10.3 and require pip 26.2.1 or newer during installation. IFEval version 3 records and checks the exact dependency versions before inference; existing results retain their earlier protocol.

## Documentation and benchmark compatibility

Keep behavior, side effects, authorization, limits and cancellation contracts in the owning
function/class docstrings. Admin Docs reads them directly; the guides retain topology,
operating procedures and recovery contracts. Benchmark implementation identities include raw
source bytes, so these docstring changes also change those identities: drain queued/running
benchmark work before deployment and preserve its original implementation until it finishes.
Retained benchmark evidence is never rewritten.

## Dependency qualification

The dependency audit initially flagged the shared virtual environment's NLTK and pip.
Qualification used an isolated environment before deployment because the shared environment
also runs LoadCoach; installed versions are recorded in the deployment result below.
NLTK 3.10.3 passes the pinned 541-case
checker fixtures in an isolated environment. Its one remaining audited advisory,
[GHSA-8mgp-746c-j5xp](https://github.com/nltk/nltk/security/advisories/GHSA-8mgp-746c-j5xp),
concerns model import/export APIs that Suite does not call; Suite uses bundled tokenizer
data and never exposes NLTK artifact paths or downloads. This is a code-path assessment,
not an upstream fix. Upgrade context: [NLTK changes](https://github.com/nltk/nltk/blob/develop/ChangeLog),
[pip release notes](https://pip.pypa.io/en/stable/news/).

## Validation

Validation: 1,509 passed and 37 skipped across two suite partitions and a passing 67-test
final closeout after the last fixes. Skips require an external PostgreSQL test DSN (30)
or a reviewed upstream Strata checkout (7). Targeted static checks, shell syntax,
all 25 maintained browser scripts' syntax and `git diff --check` passed.

## Deployment requirements

The original sweep (milestone 149) requires no new credentials, service, port, database migration or owner decision.
Existing deployment permissions apply. Deployment briefly interrupts chat, inference, tools
and voice; restarting LoadCoach unloads its models.

- **Quiet window:** the live control node benchmark store had no queued/running runs at the
  2026-10-20 inspection. Recheck immediately before rollout, pause new scheduled benchmark
  submissions and let active requests/tasks finish. Preserve submitted implementations until
  their work drains, as required by the [operating contract](agents.md#standing-permissions-and-execution-constraints).
- **Disposable installation:** the owner waived pre-deployment backups on 2026-10-20.
  Skip recovery sets and backup copies for this rollout; retain existing settings, keys,
  live SQLite bindings and benchmark evidence during the update.
- **Matching release:** review and freeze this sweep's changes in an immutable deployment
  snapshot, preserving any unrelated checkout work. Use the existing full control node deployment
  workflow; `FRONT_ONLY=1` omits the changed Turnstile and ToolLocker code. Refresh affected
  environments with the qualified dependency versions before restarting their services.
- **All installed copies:** update compute and the control node, including configured LoadCoach nodes.
  The control node CPU services (`loadcoach-front-cpu`, `spotter-front-cpu`) share the control node checkout
  but need explicit restarts. The standalone `toollocker-host` uses a separate copy under
  `~/.local/share/toollocker-host`; update its packaged host and built-in tools while retaining
  private settings, custom tools, its key and its existing bind/port. The general deployer
  does not perform those extra restarts or refresh the standalone host copy.
- **Activation:** restart the affected active services after work drains. Current active
  core services are the control node's WeightRoom, Turnstile, ToolLocker and voice gateway, and compute's
  LoadCoach, Spotter and voice service, plus the control node CPU/tool-host services above. A restart
  alone does not update separately installed source copies or dependencies.
- **Acceptance:** check service health, sign-in and authorization denials,
  source Docs, tool discovery/calls, model-file operations and retained records through live
  endpoints. Correct deployment failures in place on this disposable installation. Record delivery in the
  [milestone](agent-status.md#milestones) and remove the completed [delivery gate](agent-status.md#proposals).

## Deployment result (2026-10-20)

- **Release:** `application-sweep-20261020T181018Z`; all 384 packaged source files match on
  compute (`10.77.10.84`), control node (`10.77.10.42`) and `remote_cpu` (`kellrondgames.com`).
  Running services select the matching release explicitly; the standalone tool-host files
  and adapter copy also match. The optional same-host rig remains stopped with its next-start
  source updated.
- **Services:** all 13 affected active Suite services are healthy with no automatic restarts.
  Signed status/model/telemetry endpoints respond on all three nodes; LoadCoach/Spotter host
  fingerprints agree and unsigned node requests receive HTTP 401.
- **Live checks:** authenticated Python/JavaScript source Docs, source allowlist/privacy,
  model-file copy validation, voice readiness/gateway, adapter health and all 9 offered tools
  pass. Read-only calls work through both SSH and HTTP tool hosts; the new file-read limit
  and standalone host authentication are enforced.
- **Dependencies and regression checks:** pip 26.2.2 is installed in the application
  environments; compute/front use NLTK 3.10.3 and the control node's IFEval protocol-3 dependency check
  passes. The deployed compute environment passes 96 focused regression/documentation/quality
  checks; the existing Starlette/httpx deprecation warning remains.
- **Retained work:** 687 benchmark runs, 8,497 samples and 7,372 evidence rows remain.
  The benchmark worker is restored to its prior unpaused state and the temporary validation
  credential is revoked. No backups were created.
- **Evidence:** [machine-readable deployment checks](../data/qualification/application-sweep-20261020-deployment.json).

## Requests follow-up (milestone 150)

- **Filter parsing and queries — `loadcoach/request_filters.py`, `loadcoach/log.py`:** validate inclusive ranges and exact catalog selections, then filter before counting and pagination.
- **Access storage — `db/schemas.py`, `loadcoach/users.py`:** add atomic per-IP/account access summaries with first/last seen and accepted/rejected counters; retain known owners for expired or disabled keys without storing credentials.
- **Client attribution — `loadcoach/client_origin.py`, `turnstile/accesses.py`, app wiring:** authenticate browser-IP forwarding, isolate concurrent callers and audit rejected key attempts; preserve the peer during internal form rendering.
- **Requests UI — `turnstile/events.py`, `weightroom/pages/requests*.html`, `weightroom/static/console.js`:** add column controls, persistent filter URLs and a client-access table; source choices from Suite catalogs and the access registry.
- **Documentation and checks — source docstrings, `tests/test_request_filters.py`:** cover range validation, ownership, origin tampering/replay, browser controls and mobile layout. [Behavior and delivery evidence](loadcoach.md#request-filters-and-client-access-milestone-150).

## Clean-install acceptance

The owner plans to uninstall this disposable installation and perform a clean install once
the application is roughly 95% complete, to validate the installation process from scratch.
That is a later acceptance step, not an instruction to uninstall the current installation now.

## Dead-code cleanup (2026-10-26)

Audited tracked production Python, templates, browser assets and shell entry points using
unused-symbol scans and cross-file references. Removed 33 unused imports, six uncalled helpers,
unused local/Voice/Speech state, an obsolete internal HTTP client, duplicate native-binary
hashing and nine unused Benchmark/Docs CSS classes. No orphaned production modules or assets
were confirmed. Registered callbacks, CLI/recovery commands, compatibility exports, side-effect
imports and dynamic status styles remain; vendored code and stored data were excluded.

Validation: 1,739 passed and 38 skipped; the sole failure is the pre-existing documentation
link to missing historical qualification evidence in `docs/agent-status.md`. Production
unused-import/redefinition checks, all 32 maintained JavaScript and 11 shell syntax checks,
and `git diff --check` pass. The existing conditional exception-binding lint warning was
reviewed and retained because the caught exception is used when neither resource watcher fails.

Deployed `b541b8ff` to compute, control node and remote CPU; all 43 changed packaged files match,
as do the standalone Hermes adapter's five changed files. All 12 affected services are active
with no automatic restarts. Worker status/authentication denials, console sign-in/assets,
voice readiness and adapter capabilities pass. Restarts followed idle checks; benchmark pause
was restored to its prior unpaused state and retained counts are unchanged. The adapter's
external-health call already timed out before deployment; external backend health was not qualified.

## Page loading (2026-10-26)

Baseline: all 71 navigation/account pages plus 14 available representative detail routes were
timed through authenticated HTTP; 84 also received browser checks without JavaScript errors.
The 32.8-second Backups request blocked the console event loop, so its repeated browser baseline
was deferred. Browser requests used a temporary loopback SSH forward and cold asset requests;
HTTP measurements ran on the control node, excluding the owner's WAN/VPN latency.

- **Backups:** use lightweight schema inventory and move control node inventory work off the event loop.
  Full recovery verification remains in [the storage contract](database-storage.md).
- **Files:** read long GGUF vocabulary arrays in bounded blocks. All metadata matches across
  the control node's 65 GGUF files; standalone parsing improved from 7.9 to 3.1 seconds. The original first
  Files request timed out after 10 seconds; warm requests were already fast.
- **Benchmarks:** batch summary reads and reuse them when publishing role evidence within the
  same request. All 687 retained summaries match; scoring, permissions and retired model bindings
  remain current.

Database table sizes/counts remain exact and took about 2.4 seconds in the browser baseline.

Validation: the complete rerun passed 1,742 tests with 38 skips; its sole failure is the
pre-existing missing historical evidence link in `docs/agent-status.md`. Focused report/history
and browser checks also pass. Production lint and `git diff --check` pass.

Observed HTTP responses before → first request after deployment:

| Page | Before | After |
|---|---:|---:|
| Backups | 32.79 s | 0.10 s |
| Files | 10.05 s, HTTP 503 | 3.56 s, HTTP 200 |
| Benchmark Runs | 1.31 s | 0.57 s |

All 85 baseline routes return HTTP 200 in both after-deployment rounds. Final browser checks
cover 86 routes with no JavaScript errors or unfinished initial requests. The extra historical
recovery-set preview returns HTTP 404; it had no baseline timing. Database statistics still
take about 2.4 seconds. [Per-page HTTP/browser timings and remaining findings](../data/qualification/page-loading-20261026.json)
record initial/warm responses and measurement limits.

Deployed `44201841` to compute, control node and remote CPU; all five changed packaged files match
on all three machines. The four affected services are active with no automatic restarts;
authenticated worker status and unsigned-request denials pass. Benchmark pause was restored
and retained result counts are unchanged. The temporary audit session was revoked and its
private file removed; temporary browser forwarding is closed.


## Module audit and reasoning discovery (started 2026-10-29)

Scope: all 284 tracked, maintained Python/browser/shell modules (65,051 source lines in the reviewed inventory),
excluding tests, vendored/minified assets and external native engines. Every module received source,
control-flow, bounded-work and model/capability-coupling scans; flagged code received manual follow-up.
This is a repository audit, not qualification of every model or native architecture.
[Per-module coverage and source hashes](../data/qualification/module-audit-20261029/modules.json)
retain the exact inventory. The complete regression run had 1,828 passes, 38 skips and eight
findings; all eight corrected checks and 106 final focused checks pass. All production lint
and 46 JavaScript/shell syntax checks pass. Another 37 plugin checks and 47 engine/CPU/documentation checks passed before activation.

| Code section | Findings and disposition |
|---|---|
| Benchmarks | Expanded dense graders without changing their ASTs; retained offline evidence when a catalogue is cached. Raw implementation identities still change, so queued work must drain before activation. |
| Database | Reviewed ownership/migrations/read deadlines; added the scoped node-discovery migration through the existing schema stream. |
| Engines | Reasoning levels and tool support come from the running llama.cpp template's capabilities, with no model-name inference; unknown/empty/stale observations remain distinct. Strata's reviewed architecture restriction remains. |
| LoadCoach | Preserved admission/identity/resource contracts; removed repeated hosted-service catalogue queries and clarified probe error precedence. |
| Mirror Wall | Expanded dense runtime/store/stream logic; sized the bounded header cache to retain a full library scan and selected components. Shape/encoder compatibility rules remain necessary native contracts. |
| Plugins | Generic adapters validate bounded effort tokens; Hermes and its UI read one declared CLI schema rather than duplicate a global model-effort enum. |
| Scripts | Expanded dense install/qualification controls; no physical power operations or native-engine replacement performed. |
| SoundRoom | Native mode/repaint fields now come from family contract data; future families using those conventions need no runtime name branches. Resource/architecture limits remain explicit. |
| Spotter | Reviewed telemetry/control boundaries; existing power policy and service authority retained. |
| ToolLocker | Reviewed bounded discovery, process lifecycle and read-only tool interfaces; no model-name routing introduced. |
| Turnstile | Added per-node wake, bounded waiting, scoped retained discovery and live-copy placement preference; passive discovery/monitoring does not wake workers. |
| Voice | Reviewed transport/session/resource boundaries; Hermes controls follow the adapter's declared contract; physical audio qualification remains separate. |
| WeightRoom | Removed Home's live-status prerequisite; retained page navigation/configuration during outages, with a small alert and node-specific disabled management controls. |

Readability: 667 compressed statements in 39 modules were expanded only after exact AST-equivalence
checks. Existing architecture/file-format compatibility rules, example model names, conservative
benchmark identity matching and resource ceilings were reviewed and retained; they are not model
eligibility guesses. [Reasoning discovery contract](loadcoach.md#llamacpp-reasoning-capabilities)
and [node wake operation](README.md#where-things-run) describe current behavior.

Deployed runtime `a1ae476` on **2026-10-30** to compute, control node and remote CPU, including the
separate Hermes adapter. All 709 release files match on each host; all 12 restarted services
are active with zero automatic restarts. Inactive services remain inactive. Signed worker status
returns 200 and unsigned requests return 401 on all three nodes; adapter capabilities and Voice
administration return 200. Maintenance/drains are released, benchmark processing is unpaused,
and retained counts remain 445 completed, 102 failed and 141 cancelled.

[Deployment, page timings and browser receipt](../data/qualification/module-audit-20261029/deployment.json)
records live checks. The eight profiled pages return 200, but Models regressed from 0.67 s warm
to 2.16 s in this two-sample observation; this remains a performance limitation, not a speedup claim.
No physical power transition, actual offline-node interruption or native llama.cpp upgrade was
performed. The installed 0.7.0-dev runtime cannot establish v0.8.0 reasoning-level behavior;
new capability payloads and offline/wake/timeout behavior are covered by regression fixtures.
Wake buttons require the existing `front-wake` permission; the audited admin lacks it.
Live Chromium checks cover telemetry overview/detail, Models, Chat and all four media pages
with no page errors; telemetry fits a 390 px viewport. The temporary audit key was revoked
and browser forwarding closed.

The initial activation archive omitted Turnstile; live inventory checks caught the fallback and
activation was corrected using the package registry and Git-verified file hashes. Use the
[final release and recovery instructions](operations.md#module-audit-release-2026-10-30), not
the incomplete first archive. A final checkout documentation check is blocked by the owner's
unrelated untracked root `makeBetter.md`; it was preserved. Historical qualification receipts
are retained in the checkout, rather than copied into the code-only runtime release.
