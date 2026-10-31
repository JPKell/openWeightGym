# openWeightGym plugins

Deployment status reviewed **2026-10-16**: the framework is loaded in WeightRoom and turnstile.
The front Hermes `jeeves` adapter service is running and `hermes:gandalf` is enabled. A real
alias request and an adapter restart were qualified live on 2026-10-16. The Hermes messaging
gateway on front is stopped; plugin routing does not need it.

Plugins expose external agents and services as models at turnstile's existing `/v1` door.
The first plugin wraps an existing Hermes installation using its CLI. Plugin requests work
without compute; a model request made by the external agent can independently wake compute.

Background native-model snapshot refreshes send `x-turnstile-no-wake` and keep the last valid
snapshot while compute sleeps or is unavailable. Turnstile's plugin-mirror endpoint also refuses
implicit wake-ups. Account permissions remain front-owned and are checked on each request.

The contract and acceptance criteria are in [the specification](../docs/plugins/SPEC.md).
Hermes-specific options are in [hermes/README.md](hermes/README.md).

## Use a plugin

1. Give an account `front-plugins` to use adapters, and `front-manage-plugins` to configure
   them. Superusers have both. Ordinary openWeightGym model restrictions also apply to public aliases.
2. Open **Settings → Plugins**. Valid folders are listed; invalid manifests show their reason.
3. Enable a plugin and add a named instance with its HTTP URL, listening address/port, and
   plugin settings. Each instance gets a separate pairing key stored in the front machine's
   `keys/plugins/` directory, never in its ordinary settings file.
4. Download that instance's Linux installer. Extract it on the machine hosting the external
   service, read the root `README.md` beside `install.sh`, and run `./install.sh` as that
   machine's normal login user. It installs the HTTP adapter and its own environment and
   systemd user service. It does not install Hermes or change its profiles.
5. Synchronize settings and discover profiles/models. Select entries and give them unique
   public names, such as `hermes:gandalf`. These names appear in turnstile's model list marked
   **Plugin**, and in openWeightGym's model/chat lists.

The bundle's [installation guide](INSTALLER-README.md) includes prerequisites, service
and foreground commands, preserved files, and recovery from failed installs. The installer
copies the shared `db` package, including migrations and runtime schemas, alongside `plugins`.
This fixes the earlier `package directory 'db' does not exist` build failure. Regenerate the
instance bundle after deploying updated WeightRoom code; existing archives retain the defect.

Settings save locally first. Desired and applied revisions show whether the remote service
has accepted the change. Pending updates are retried on reconnection. Changes to the adapter's
listening address or port need its service restarted on that machine; Hermes health controls
operate Hermes gateway commands, rather than restarting the adapter itself.

Health checks are optional. Automatic start/restart is disabled initially and can only run
when the plugin declares the capability and the corresponding action is allowed in settings.
Disabling a plugin stops new routing and hides its models; answers already in flight finish.

## Chat API

Send ordinary OpenAI-compatible requests to turnstile:

```json
{"model":"hermes:gandalf","messages":[{"role":"user","content":"Hello"}],"stream":true}
```

Missing `X-Suite-Session-Mode` means a fresh run. To continue a session, send
`X-Suite-Session-Mode: persistent` and the `X-Suite-Session-ID` returned earlier. Persistent
mode without an ID creates a session. Every accepted chat response identifies its adapter,
instance, session and request in HTTP headers. Session IDs are opaque and bound to the openWeightGym
account, adapter instance and underlying profile, independently of the public alias.

Agent progress appears in `delta.plugin_events`; reasoning, when available, appears in
`delta.reasoning_content`. Hermes's current CLI emits text previews without identifying
which tool round they belong to. These previews are progress events; its final `result.text`
is the assistant content. This preserves correct responses for ordinary clients while openWeightGym
shows all available events. Tool events describe work Hermes performs internally, rather than
tool calls the caller should execute. Token/cost reports from adapters are omitted.

The generic adapter accepts `reasoning_effort` as an optional 1–64 character token
(letters, digits, dots, underscores or hyphens; first character alphanumeric), rather than
imposing one agent's level names on every plugin. Omitted/null means no request override;
each driver validates its native contract. Hermes uses the single enum in its settings schema
for global/profile settings, CLI requests and Voice controls. An unavailable schema is unknown,
not an empty list of supported levels; existing Voice values remain visible without being
advertised as verified choices. Runtime model reasoning capabilities are a separate contract.

`Idempotency-Key` prevents a retried request from executing twice. Completed requests can
return their saved result. Running or uncertain requests return a conflict. Disconnection
cancels the subprocess tree; interrupted work is never automatically replayed.
Use your openWeightGym key with `GET /turnstile/plugins/runs/PLUGIN/INSTANCE/REQUEST_ID` to inspect an
owned request receipt after a connection failure, without submitting the work again.

Turnstile keeps minimal adapter-use records in front's `data/plugins.db`: caller, public model,
plugin, instance, request/session IDs, time and outcome. It does not capture prompts, answers,
tool output or token totals in this log. The Plugins page includes the authorized activity
view. Normal compute calls made by an agent retain compute's normal logs and usage.

## Add another plugin

Create a folder with `manifest.json`, `schema.json`, `adapter.py`, and a README. The manifest
declares protocol version 1, an ID matching its folder name, a display name, capabilities,
and the relative settings-schema and driver filenames. Folder scanning reads data only;
it does not import or execute plugin code. An explicitly installed adapter server loads its
own driver and implements the common HTTP endpoints through `plugins._runtime`.

For example, a minimal manifest is:

```json
{"protocol":1,"id":"example","name":"Example service","version":"1.0.0",
 "capabilities":["send","receive","models"],"schema":"schema.json",
 "driver":"adapter.py","class":"Example"}
```

An HTTP driver can implement `models(settings)` and `events(run, settings)` using an HTTP
client, with the last event `{"type":"result","text":"final answer"}`. A CLI driver uses
argument vectors and terminates its children on cancellation; Hermes is a working example.
Declare only implemented capabilities. Use the optional manifest `files` list for additional
driver resources; paths stay inside the plugin folder, and runtime state/keys are excluded.
Settings schemas must be self-contained JSON Schema 2020-12 with local references. The
settings page renders top-level fields; structured values are editable as JSON.

Import the public interface with `from plugins import Adapter, Run`; the contract is in
[_sdk.py](_sdk.py). Its asynchronous event iterator emits
normalized session, preview, reasoning, tool, result and error events. The runtime handles
authentication, session ownership, concurrency, request receipts, settings persistence and
OpenAI response formatting. Plugin-specific CLI or HTTP calls belong inside its own folder.
Framework module names start with an underscore, which valid plugin IDs cannot use, so
even a folder named `sdk` or `runtime` cannot shadow the runtime during discovery.

Only trusted operators should add executable plugin folders. The remote adapter runs as its
installation user, and the external agent retains the selected profile's workspace, memory,
tools and credentials. Separate openWeightGym sessions do not isolate a shared Hermes profile's
filesystem or profile memory.

## Development and verification

Run `python -m pytest tests/test_plugins.py tests/test_plugin_runtime.py tests/test_plugin_turnstile.py`
and `tests/test_plugin_front.py`. Tests use an executable Hermes stand-in and actual HTTP
streams, exercise compute-unavailable routing without power operations, and inspect/extract
the downloadable bundle. An additional HTTP-service plugin verifies
that new drivers need no turnstile-specific code. Live installer/Hermes checks are reported in
the specification separately from automated fixture tests.

Adapter-local `data/runtime.db` stores owner/profile/native-session mappings and durable
request receipts. Completed receipts retain final responses/events for idempotent replay;
prompts are represented only by a hash, and accounting is excluded. Hermes retains its own
native sessions according to its configuration. openWeightGym web-chat transcripts and returned
session/event metadata live in front's existing `data/chat.db`.

Runtime state (`*/settings.json`, pairing keys and adapter databases) must stay outside source
control. Deployment preserves these files. The plugin folder, README and specification ship
with the application; installed adapters also receive the plugin's manifest, schema and driver.
