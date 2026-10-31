# External model plugins, protocol version 1

Status reviewed 2026-10-16: implemented, deployed and live-qualified. The framework is deployed
in WeightRoom and turnstile; Hermes instance `jeeves` runs on front. On 2026-10-16 a real
`hermes:gandalf` request through turnstile returned 200 with adapter, instance, profile, session
and request headers. After `systemctl --user restart suite-adapter-hermes-jeeves` a second request
also passed. Earlier evidence: the disposable Linux installation and Gandalf continuation check.

## Scope and ownership

Turnstile routes registered adapter aliases directly to independent HTTP adapter servers. It
continues forwarding ordinary model/tool requests to compute. Since milestone 76 the openWeightGym's
users, groups, keys and permissions are kept on the front machine, and turnstile enforces them there.
Authentication and catalog reads never wake compute. Downstream requests made by Hermes can
use the ordinary turnstile wake path.

The front machine owns plugin settings, model aliases and minimal adapter-use logs. WeightRoom
provides the management pages. The adapter server owns its executable invocation, native
session mapping and request receipts. Existing web chat remains hosted inside WeightRoom and
uses front's chat database; no separate chat service is introduced.

## Discovery and installation

The project root contains `plugins/`, with one folder per plugin. Required files are a valid
versioned `manifest.json`, a JSON settings schema, a driver source file and a README. Plugin
IDs and instance IDs are bounded slugs. Referenced files must remain inside their plugin
folder. Invalid folders are visible with validation errors and cannot be enabled. Discovery
never imports executable code. An installed server loads only its selected plugin's driver.

A plugin can have multiple named instances. Each instance has a URL, independent pairing
key, plugin settings, health policy, discovered models, alias selections and settings revision.
Public aliases are unique and prefixed with the plugin ID. For Hermes, the underlying entry
is a Hermes profile, selected with `hermes -p PROFILE`, not an underlying LLM model override.
Native profile names survive public alias renames. Discovery refresh preserves existing
aliases and enablement; newly found profiles start disabled.

The downloadable Linux installer contains only the shared adapter runtime, the selected
plugin, standalone dependency metadata, initial instance configuration and its pairing key.
It installs its own Python environment and systemd user service, using the configured
listening address/port. It connects to an existing Hermes CLI/venv/repository. It neither
installs Hermes nor copies unrelated openWeightGym databases, keys, settings or plugins. Reinstallation
preserves existing configuration and session state. The bundle is available only to plugin
managers and must not be cached by shared HTTP caches.

The bundle includes a root `README.md` beside `install.sh` with prerequisites, options,
service checks and troubleshooting. The 2026-10-14 local packaging fix copies the shared
database modules into the install destination and includes connection, migration and schema
dependencies; older archives fail wheel setup with `package directory 'db' does not exist`.
Seventeen focused plugin/documentation checks and a fresh disposable pip installation,
isolated SQLite runtime initialization and state-preserving reinstallation passed. These
checks used the Hermes fixture, not a real Hermes request or the user's adapter host.
Deploy updated WeightRoom and regenerate the instance archive before retrying an old failed
installation. Existing settings, keys and receipts are preserved.

## Capabilities and adapter HTTP contract

Capabilities: `send`, `receive`, `models`, `health`, `start`, `restart`. Sending and receiving
are required for a model plugin; other operations are exposed only when declared. All routes
require the instance's bearer pairing key. The shared runtime exposes:

| Route | Behavior |
|---|---|
| `GET /adapter/v1/capabilities` | Protocol, plugin and supported capabilities |
| `GET /v1/models` | Underlying profile/model entries |
| `POST /v1/chat/completions` | Standard completion or SSE chunks, with adapter metadata |
| `GET /adapter/v1/settings` | Applied revision, public settings and restart requirements |
| `PUT /adapter/v1/settings` | Validate and atomically persist a desired settings revision |
| `GET /adapter/v1/health` | Adapter heartbeat and external-service health |
| `POST /adapter/v1/control` | Explicitly permitted `start` or `restart` |
| `GET /adapter/v1/runs/ID` | Owned request status for an uncertain/disconnected caller |

openWeightGym callers access receipts through `GET /turnstile/plugins/runs/PLUGIN/INSTANCE/ID` with
their openWeightGym key; turnstile supplies the private pairing credential and enforces current model
permissions. Adapter receipts include profile and status; completed receipts retain responses
and events for idempotent replay. They do not store prompts or accounting. Native histories
remain in Hermes; openWeightGym web-chat histories remain in front's existing chat database.

The turnstile supplies a trusted `X-Suite-User-ID`; caller-supplied identity and adapter credentials
are never forwarded. HTTP calls use fixed configured endpoints, not URLs supplied in chat
messages. User messages are passed to CLI processes using a UTF-8 query file and argument
vectors, never interpolated into shell command text.

## Authorization, limits and logs

`front-plugins` permits adapter use; `front-manage-plugins` permits configuration, discovery,
installer downloads and health controls. Superusers have both. The effective group/app model
allowlist applies to public aliases. Since milestone 76 the accounts are on the front machine:
turnstile checks each key there on every request, so key expiry, disabled users, app restrictions
and revocations apply at once, whether or not compute answers. Compute's snapshot holds only its
model list, for the catalog while compute is unreachable; its `keys` list is always empty. There
is no anonymous fallback for adapters.

Per-user request limits are enforced by turnstile; each adapter separately limits subprocesses.
Adapter-use logs contain identity, plugin/instance/profile/public alias, request/session IDs,
timestamps and outcome. They exclude prompt/answer/tool contents and token/cost accounting.
Readers see their own uses; `see-log` permits all. Sensitive or restricted requests require an
instance explicitly permitted for that sensitivity; the default is normal only. Unlogged
requests obey the synchronized permission and setting and create no adapter-use log entry.

## Settings and health

Settings are schema-validated before a local atomic save. A new desired revision becomes
pending. The front controller sends the full revision through HTTP and records the acknowledged
applied revision. Updates retry on reconnection; chat dispatch refuses an unapplied desired
revision. Equal revisions are idempotent only for equal settings; stale/conflicting revisions
are refused. Active runs use their original settings snapshot. Listen address/port changes
are persisted but reported as requiring adapter-service restart; they are not silently claimed
to have changed the running listener.

Health polling is optional. It records heartbeat and external-service state. Auto-start and
auto-restart are separately selected, require corresponding allowed actions and declared
capabilities, and use a cooldown. No recovery action automatically repeats a chat request.
Hermes health uses `gateway status` and `gateway list`; control uses `gateway start` or
`gateway restart` with a configured profile. CLI chat itself does not require a running gateway.
An unreachable HTTP adapter cannot restart itself through that endpoint; its installed OS
service provides process recovery.

## Session and response behavior

Missing `X-Suite-Session-Mode` defaults to fresh. Persistent mode without an ID creates a
session. To resume, the caller sends persistent mode plus `X-Suite-Session-ID`. Every accepted
response has `X-Suite-Adaptor`, `X-Suite-Instance`, `X-Suite-Session-ID`, and
`X-Suite-Request-ID`. Opaque IDs map to native session IDs and bind to account, instance and
profile. Another user's ID, another profile or another instance is refused. Runs on the same
session are serialized/refused while busy; unrelated sessions can run concurrently.

Fresh CLI queries contain the supplied history and current input. Persistent continuation
uses the existing Hermes session and new input, rather than inserting previous history again.
An edited/regenerated openWeightGym turn starts a fresh adapter session; prior tool effects cannot
be rewound. Profile memory/workspace remains shared according to Hermes's own configuration.

Streaming preserves all available events. Reasoning uses `reasoning_content` where present;
agent text previews, status, tool starts/results and other non-usage events use `plugin_events`.
The final assistant content is the actual final result. Tool events are observational and
never instruct the API client to execute the tool again. Available inline images are supported
according to plugin capabilities; unsupported input types/options receive explicit errors or
a response header identifying ignored generation parameters. The current Hermes CLI lacks
reasoning events and final-round text boundaries; this limitation is reported rather than
filled with fabricated reasoning or an incorrect concatenation of tool-round previews.

`Idempotency-Key` is scoped to account/profile and paired with an input fingerprint. Reuse
with different input is refused. Completed requests can replay the recorded final response;
running, failed, cancelled or uncertain requests are never executed again under the same key.
Subprocess cancellation terminates its process group and collects its children. On server
restart, surviving running receipts become uncertain. Automatic retries of agent work are
prohibited. Runtime errors are returned in OpenAI error envelopes or a final SSE error event.

## Hermes settings

Settings include CLI path or venv, repository, optional Hermes home/workspace, provider/model
overrides, reasoning, skills, toolsets, maximum turns, run budget, worktree, pass-session-ID,
ignore flags/safe mode, and profile-specific overrides. Approval policy is explicit per
profile: use the native profile's noninteractive approval rules by default, optionally enable
`--yolo` and/or `--accept-hooks`. Native auto-approval settings remain effective in profile
mode; the wrapper supplies closed input, not an interactive approval bridge. Requests cannot
change these policies. Inherited bypass environment flags are removed. The adapter uses only the supplied
chat, profile, session and gateway commands; it never sets a sticky default profile.

## Acceptance criteria

- Adding a valid plugin folder needs no plugin-specific turnstile routing code.
- Invalid or disabled drivers are not imported by front/turnstile or offered; aliases and instance mappings
  remain stable through discovery and public renaming.
- Known authorized callers can list/use adapters with compute unavailable, without waking it.
  Unknown/expired/revoked/app-restricted keys and disallowed aliases are refused.
- Local/remote settings, desired/applied revisions, offline pending updates, health policies
  and installer contents are verified; secrets never appear in settings responses or logs.
- CLI arguments preserve literal special characters. Fresh/persistent sessions, ownership,
  concurrent turns, cancellation, idempotency and restart uncertainty are exercised.
- Actual HTTP streaming exposes available agent events and yields the correct final content.
  Adapter responses/logs contain no token/cost metrics.
- openWeightGym's model/chat UI recognizes adapter entries and persists returned session/event metadata.
- Existing compute/voice/tool behavior remains covered by its regression checks. No power
  operations on the current workstation are used as tests.

## Verification evidence and limits

Automated checks exercise a real executable Hermes stand-in, actual HTTP streaming, session
ownership/busy rejection, capacity, cancellation of child processes, idempotent replay,
failed/uncertain receipts, instance locking, pending/applied revisions, profile policies,
permissions/revocation/expiry, literal queries, management pages and web-chat continuation.
A separate HTTP-service driver passed the same runtime/turnstile path without Hermes routing
changes. The core chat/front/auth/model regressions passed (188 tests), and supplementary
settings/ToolLocker/voice-Hermes checks passed (34 tests, including overlapping plugin checks).
After final module and offline-UI changes, all 55 plugin/turnstile/WeightRoom checks passed,
including fresh-process discovery of a plugin named `sdk` without executing its code.

The downloaded Linux bundle was extracted and installed into a disposable directory on
`10.77.10.42` using its own venv, without registering a service or restarting openWeightGym.
Live discovery found `gandalf`; it returned a streamed answer and successfully recalled a
verification code on native session continuation. Responses omitted usage. Temporary adapter
files and test-owned Hermes sessions were removed. Installed Hermes customizations printed
non-JSON diagnostics between events; the wrapper now discards bounded diagnostics and tests
that behavior. It still rejects malformed JSON event objects and missing final results.

No compute power transitions were tested. The installed CLI exposes no reasoning events,
so live reasoning availability could not be verified. Linux installation/runtime was exercised;
systemd service registration was reviewed and packaged but not activated during verification.
Windows/macOS installers are outside this first version. Listener changes require a service
restart on the adapter machine. Offline authorization uses the last complete snapshot and
cannot observe revocations until synchronization returns. Deployment and enabling a live
instance remain separate operational steps.
