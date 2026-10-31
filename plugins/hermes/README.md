# Hermes adapter

Deployment status reviewed **2026-10-16**: front's `jeeves` instance is configured and its
adapter service is running. It discovered ten profiles, synchronized its settings and enabled
`hermes:gandalf`. Adapter heartbeat passes; the Hermes messaging gateway reports stopped.
Live inference through the alias remains to be checked after the compute benchmark sweep.

This plugin wraps an existing Hermes Agent CLI on the adapter's machine. An exposed model
such as `hermes:gandalf` selects `hermes -p gandalf`; the suffix is a Hermes profile. The
profile's own underlying provider/model is preserved unless its adapter settings override it.

## Configure an instance

In **System → Plugins → Hermes Agent**, add a named instance with its HTTP URL. Configure
the existing Hermes `venv` and `repository`, or a `cli` executable. A configured venv takes
precedence and runs `venv/bin/python -m hermes_cli.main` directly. No activation shell is used.
Optionally set `hermes_home` and a workspace. `--in` scopes native sessions to that workspace.

Download and install the instance's Linux bundle on the same or another machine. The bundle
has its own adapter environment/service and a dedicated pairing key; it does not install
Hermes, select a sticky profile, or alter `~/.hermes/config.yaml`. Keep its archive private.
Read the root `README.md` beside `install.sh` for prerequisites, service commands and
troubleshooting. If an older installer failed with `package directory 'db' does not exist`,
download a regenerated bundle from updated openWeightGym code and reinstall in the same directory;
existing settings, pairing keys and session receipts are preserved.
After installation, synchronize settings and discover profiles. Existing aliases survive
discovery refreshes, and new profiles start disabled until selected.

Common options are documented beside their fields in [schema.json](schema.json): model/provider,
reasoning, skills/toolsets, maximum turns, run budget, workspace/worktree, session-ID injection,
ignore flags and safe mode. `max_turns: 0` preserves Hermes's own profile limit. The default
300-second run budget can be changed. The instance's `max_concurrent` bounds simultaneous runs.

`profiles` supplies per-profile overrides. For example:

```json
{
  "gandalf": {"workspace": "/home/user/stories", "approval": "profile"},
  "builder": {"workspace": "/home/user/project", "approval": "yolo", "accept_hooks": true, "worktree": true}
}
```

`profile` keeps the native profile's approval rules, supplies closed interactive input and
adds no approval-bypass flags. Hermes's native `approvals.mode` and `single_query_mode` still
apply; closed input alone does not override a profile configured to auto-approve. `yolo` explicitly adds
`--yolo`; `accept_hooks` explicitly adds `--accept-hooks`. Ordinary callers cannot enable these
through the request body. Native Hermes/profile rules still govern what the agent can do.
Profiles share their configured memory, files and credentials even when sessions differ.

## Commands and sessions

Discovery uses `hermes profile list`, with canonical IDs extracted from JSON or Hermes's
table. Chat uses `hermes -p PROFILE chat --oneshot --quiet --format stream-json --source tool
--query-file PATH`, plus configured overrides. Query files preserve quotes, backticks and
`$(...)` literally. Fresh runs receive supplied conversation context. Persistent sessions
use a named `suite:...` session with `--create-if-missing`, then resume its native ID.
Sessions are bound to openWeightGym account, instance and profile. Every accepted response returns
the opaque `X-Suite-Session-ID`; clients resume with it and persistent mode.

Up to two inline PNG/JPEG/WebP images can be passed using temporary files and `--image`.
HTTP image fetching and caller-supplied tool definitions are unsupported. Unsupported sampling
parameters are identified in `X-Suite-Ignored-Parameters`; profile model configuration controls
them. The wrapper does not synthesize token accounting or use `--usage-file`.

Hermes's current stream-json emitter exposes initialization, text previews, tool starts/results
and a terminal result. It does not expose reasoning events. All available events are forwarded;
the actual final text becomes the assistant answer. Intermediate text is kept as preview events
so tool-round text cannot be confused with a final answer. Future reasoning events are supported.
Bounded non-JSON diagnostics from Hermes customizations are discarded even when interleaved
with events; malformed JSON objects or a missing final result fail the request.

Cancellation or a timeout terminates the CLI process group. Durable request receipts prevent
automatic repeat execution. A server restart marks abandoned work uncertain. A fresh run means
no earlier session is resumed; Hermes may still save that run in its own session store.

## Health controls

The optional health check uses `hermes -p HEALTH_PROFILE gateway status` and
`hermes gateway list`. Start/restart controls use the matching gateway commands, gated by
explicit allow settings. Automatic recovery is off initially and respects cooldown and active
runs. A stopped messaging gateway can coexist with working CLI chats; both states are distinct.

The adapter's own systemd user service provides process recovery. Its HTTP endpoint cannot
restart an unreachable adapter. Changing `listen_host`/`listen_port` reports restart required:
apply using the old connection URL, restart the adapter service locally, then update the front
connection URL if necessary. Plugin settings updates use HTTP and require no SSH access.
