# ToolLocker setup and adding tools

ToolLocker is the MCP gateway for tools such as reading files, searching code and calling your
own Python functions. A client connects to `http://FRONT:8443/mcp` with an openWeightGym API key.
Turnstile checks the account and signs the request for the private gateway on port 9443.
Tools run on the machine that owns their files or service; the gateway runs on front while
compute is off. An MCP-capable client or agent executes tool calls; **Settings → Tools → Try a
tool** checks them independently of a model.

These same instructions are available in the console at **Docs → Modules → ToolLocker** (`/docs/modules/toollocker`).

Removing the account’s `use-tools` ability stops tool calling through openWeightGym model requests. An MCP client talking to a cloud model directly can still call ToolLocker. To cut off server tools entirely, remove the account’s ToolLocker tools with its `tools` patterns.

## Installer defaults

For a fresh installation, choose one of these options in `./install.sh`:

| Main installer choice | Command-line option | ToolLocker |
|---|---|---|
| Console only | `--topology front` | Installed on the console with WeightRoom and Turnstile. |
| Everything on this machine | `--topology all` | Installed alongside the console; uses the front account store. |
| GPU node | `--topology compute` | Gateway omitted. Tools hosted here are connected to the front gateway separately. |

New front installations write `settings/toollocker.toml`, configure local MCP routing in
`settings/weightroom.toml`, and install `toollocker.service`. The gateway inherits the console's
SQLite or PostgreSQL accounts/event store; it needs no local LoadCoach or separate identity
database. The installer creates `data/shared/` and registers these bundled servers:

| Server | Default resource on front | Offered tools |
|---|---|---|
| `files` | The installation's `data/shared/` folder | `files__list_folder`, `files__read_file` |
| `code` | The installation's Git repository | Repository listing, search, status, history and changes under `code__*` |

The caller's permissions still determine which tools are offered and callable. A tool hosted on
an unavailable machine remains unavailable even when the front gateway is running.

The lower-level `scripts/install-suite.sh` assigns ToolLocker to its `front` and `all` presets,
including `--services front` and `--preset front`; its `compute` preset omits the gateway.
Its front preset also selects the voice gateway. To select only the core front services, use
`--services weightroom,turnstile,toollocker`. Voice is optional in the main installer.

For a new installation, continue at [Verify the connection](#verify-the-connection). For an
existing console without ToolLocker, use [Install ToolLocker on front](#install-toollocker-on-front).

## Why tools can be missing

Earlier console-only installs omitted ToolLocker. Update and Repair retain their saved service
selection and existing settings, so they do not automatically add or relocate a gateway. A
compute-owned gateway needs the [migration steps](#moving-a-compute-owned-gateway).

Three things must be present: a running gateway, at least one configured MCP server, and permission
for the caller to see its tools. An empty recorded catalogue alone does not identify which is missing.

## Requirements

| Part | Requirement |
|---|---|
| Gateway | The installed openWeightGym checkout and its Python 3.13+ environment; `toollocker` is included in the base package. Use the same service account as the console. |
| Authentication | Existing `settings/weightroom.toml`, its accounts/event database and Turnstile's machine-key file. `front = "weightroom.toml"` inherits them; a separate LoadCoach or accounts database is unnecessary. |
| PostgreSQL | If the console uses PostgreSQL, its database must be reachable and the environment needs the project's `postgresql` extra. A console already using PostgreSQL has this dependency. |
| Services | Linux with the console account's systemd user manager. The account needs access to the installation and tool files. |
| Ports | Turnstile owns public port 8443; the front gateway listens on `127.0.0.1:9443`. Keep 9443 private. Clients use the trusted LAN/VPN or an HTTPS proxy for bearer-key access. |
| Tool servers | At least one MCP server: a fixed stdio command, an authenticated HTTP address, or an explicitly public HTTPS address. Install each server's own dependencies in its runtime. |
| Included tools | File tools need an explicitly allowed folder. Code tools also need `git` and an allowed repository. Neither requires a GPU or inference model. |
| Client access | An enabled account/key, a matching group/account tool pattern, a sufficient sensitivity ceiling, and any selected tool set containing the tool. Administrators need `manage-settings` for the Tools page. |

The separate lightweight remote tool host needs Python 3.11+; its built-in tools use the standard
library. The full gateway uses the project's Python 3.13+ environment.

## Install ToolLocker on front

Run these steps on the console machine as the account that already runs WeightRoom and Turnstile.
Use the updated installer checkout for these steps, which add the gateway to older or custom installs.
Examples assume a per-user installation at `~/openWeightGym`. For a system installation, the
checkout is `/opt/openweightgym`, configuration belongs to `openweightgym`, and its user-service
files are under `/var/lib/openweightgym/.config/systemd/user`. Use that account's service manager,
not root's. The console's existing keep-running/linger setting also covers ToolLocker.

### 1. Check what is installed

```bash
cd ~/openWeightGym
systemctl --user status toollocker.service
systemctl --user cat toollocker.service
.venv/bin/toollocker --help
```

`Unit toollocker.service could not be found` means the service still needs creating. An inactive
existing unit needs its configuration checked before starting. If the executable is missing,
repair the installed Python environment with `owg repair` first. The generic service template's
paths may differ from this installation; use the actual checkout path from `pwd`.

### 2. Configure a first local tool server

If `settings/toollocker.toml` is absent, the installer in step 4 writes the bundled server
configuration automatically. To configure it by hand, use the example below. For an existing
file, merge the front ownership settings and preserve its server names, levels and tool sets.
Replace `/home/you/openWeightGym` with the absolute path printed by `pwd`; TOML command arguments
do not expand `~` or `$HOME`.

```toml
front = "weightroom.toml"
host = "127.0.0.1"
port = 9443

[servers.files]
command = [
    "/home/you/openWeightGym/.venv/bin/python", "-m", "toollocker.tools.files",
    "/home/you/openWeightGym/data/shared",
]
description = "Shared files on the console"
level = "normal"

[servers.code]
command = [
    "/home/you/openWeightGym/.venv/bin/python", "-m", "toollocker.tools.code",
    "/home/you/openWeightGym",
]
```

Use `front`, omitting `loadcoach`: this shares the console's existing SQLite or PostgreSQL accounts
store and signing key. The included file tools skip hidden paths, paths without the other-read
permission bit, and symlinks escaping the allowed folder. Keep the allowed folder separate from
settings, keys and databases.

### 3. Route MCP to the front gateway

In `settings/weightroom.toml`, add or update these keys **inside the existing `[turnstile]` table**:

```toml
[turnstile]
keep_console_settings_on_front = true
toollocker_url = "http://127.0.0.1:9443"
```

Keep the rest of that table, including its machine key and ports. Ensure its existing `ports`
list includes `8443`; preserve any voice and other configured ports. Avoid adding a second
`[turnstile]` table. Turnstile continues to own 8443; ToolLocker must use 9443.

### 4. Install the gateway service

From the checkout, select just ToolLocker so the installer does not add other optional services:

```bash
scripts/install-suite.sh --services toollocker --database keep \
  --non-interactive --yes --no-start
```

`--database keep` preserves the existing store bindings. `--no-start` installs and enables the
unit without starting it; the installer writes its executable/settings paths and `tooldoor.service`
alias. Existing configuration is kept, and replaced service units are backed up. Review custom
units and drop-ins with `systemctl --user cat toollocker.service` before replacing them.

Then start or restart ToolLocker and reload the console's routing:

```bash
systemctl --user restart toollocker.service
systemctl --user restart turnstile.service weightroom.service
systemctl --user is-active toollocker.service turnstile.service weightroom.service
journalctl --user -u toollocker.service -n 50 --no-pager
```

Restart after active requests finish: restarting Turnstile interrupts requests it is carrying.
Installing just the gateway does not require starting a compute node. The standalone
`toollocker-host.service` is optional when a local stdio command runs the tools.

## Moving a compute-owned gateway

Installing on front does not move an existing compute gateway's settings or records. Preserve
the server names, descriptions, sensitivity levels, tool sets and per-tool overrides. Replace
compute-local stdio paths with a fixed SSH command or a remote MCP host so tools still execute
where their files and dependencies live; see [Remote tool hosts](#remote-tool-hosts).

Settings changed through the console and recorded tool schemas live in the owning database,
separately from `toollocker.toml`. Follow **Front ToolLocker relocation (milestone 128)** in
`docs/operations.md` for the verified store transfer, cutover and rollback procedure. Verify the
front gateway's tool list, permissions and calls before retiring the old service. The installer
does not disable a legacy compute gateway automatically.

## Verify the connection

For the bundled file server, create a sample in its default allowed folder on front:

```bash
cd ~/openWeightGym
printf '%s\n' 'ToolLocker is working.' > data/shared/example.txt
chmod 644 data/shared/example.txt
```

Use your installation's checkout path for a system or custom install. If the file server uses a
different root, place the sample there instead.

Open **Settings → Tools → Try a tool → Connect**. Expect `files__list_folder` and `files__read_file`.
Call `files__list_folder` with an empty folder to list the allowed roots, then read the sample
file by its absolute path. Recorded descriptions appear after gateway discovery.

For an API check, set `OWG_API_KEY` to a key from **My account**, then run on front:

```bash
curl -fsS http://127.0.0.1:8443/mcp \
  -H "Authorization: Bearer $OWG_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Use public port 8443, including for local checks. Port 9443 accepts Turnstile's signed identity,
not a browser's API key. No separate ToolLocker account/key is needed.

On **Settings → Groups**, allow the needed names or patterns, such as `files__*`, and check the
group's maximum sensitivity. Account tool overrides can narrow or extend that access; hidden
patterns remove tools. Tool sets such as `/mcp/reading` narrow the permitted list further. Test
with the account the client actually uses, then refresh its MCP tool list.

## Add a Python tool

This example adds a harmless greeting tool on front. From the checkout, create `data/tools/greeting.py`:

```bash
mkdir -p data/tools
cat > data/tools/greeting.py <<'PY'
from toollocker.tools import tool


@tool
def greet(name: str = "world") -> str:
    """Return a greeting for the supplied name."""
    return f"Hello, {name}."
PY
```

Check discovery and execution directly, before registering it with the gateway:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"greet","arguments":{"name":"Ada"}}}' \
  | .venv/bin/python -m toollocker.tools "$PWD/data/tools"
```

Expect `greet` in the list and `Hello, Ada.` in the result. Add a new section to
`settings/toollocker.toml`, again replacing the installation path:

```toml
[servers.example]
command = [
    "/home/you/openWeightGym/.venv/bin/python", "-m", "toollocker.tools",
    "/home/you/openWeightGym/data/tools",
]
description = "Custom tools on the console"
level = "normal"
```

Run `systemctl --user restart toollocker.service`. Connect again in **Settings → Tools** and call
`example__greet` with `{"name":"Ada"}`. Grant `example__greet` or `example__*` to the intended group
if needed. Put future custom files in this tool folder; changing them requires another gateway
restart because it owns the local tool process. Keep custom tools outside the shipped package
so updates preserve them.

## Tool function contract

- Mark exported functions with `@tool`; each tool name must be unique within its folder.
- Describe the purpose, arguments and result in its docstring: this is what the model reads.
- Annotate parameters as `str`, `int` or `bool`. Parameters without defaults are required.
- Return text. Results are cut after 40,000 characters. Raise `ValueError` for a refusal the
  model can understand; other exceptions are reported as tool failures.
- Optional `setup(settings)` receives its module's `[NAME]` section from a TOML settings file.
  Register the host command with `--settings /absolute/path/tools.toml`; a module needing setup
  is omitted when its section is absent or setup fails.
- One call runs at a time on a tool host. Install extra Python dependencies in the environment
  named by its `command`.
- Tools run with their service account's filesystem and network permissions. Bound access to
  explicit resources; avoid model-supplied shell commands. Assign tools that modify data an
  appropriate `sensitive` or `restricted` level before granting access.

## Add other MCP servers

Add one `[servers.NAME]` section to the gateway configuration. Use `command = ["/absolute/program", "argument"]`
for an MCP stdio server. The command is fixed operator configuration; tool arguments travel over
MCP, not through a shell. For code tools, use `.venv/bin/python -m toollocker.tools.code` followed
by absolute repository paths; `git` must be installed on that host.

For an HTTP server, use `url = "https://HOST/mcp"` and `key_file = "../keys/HOST.key"` instead of
`command`. The owner-readable key file contains that upstream server's bearer key, not the
console's machine signing key. Only intentionally unauthenticated HTTPS servers use `public = true`
instead of `key_file`. Review discovered tools, levels and account access before enabling a server.
Restart ToolLocker after changing its server configuration.

## Remote tool hosts

Run `scripts/install-tool-host.sh` on the machine that owns the files, from a copy of the project:

```bash
scripts/install-tool-host.sh --name nas --files /srv/shared \
  --bind 192.168.1.20 --address 192.168.1.20 --compute 192.168.1.10
```

Here `192.168.1.20` is the tool host and `192.168.1.10` is the front gateway. The script's legacy
`--compute` flag names the permitted gateway host for its printed firewall example. It creates
`toollocker-host.service`, a private host key, tools under `~/.local/share/toollocker-host/tools`
and `~/.config/toollocker-host/tools.toml`. Register the printed `[servers.nas]` section and a
private copy of its host key on front. Allow only the gateway host to reach the tool-host port
(default 9100); use a trusted private network or secure transport for HTTP bearer keys.

Put custom Python files beside the tool host's `files.py` and `code.py`; add any setup sections to
its `tools.toml`. Restart `toollocker-host.service` there, then `toollocker.service` on front.
Do not edit the shipped tool files: the host installer replaces those on update while retaining
custom files and settings. A fixed SSH stdio command is also supported when front already has
SSH access to the file-owning host.

## Troubleshooting

| Symptom | Check |
|---|---|
| Unit not found | Add ToolLocker with the installer command above; Update/Repair of an older console-only selection does not add a missing service. |
| Tool service has not reported configuration | Check `systemctl --user status toollocker` and its journal, then `keep_console_settings_on_front`, `toollocker_url` and port 8443 in the console configuration. |
| Gateway will not start | Check absolute command paths, Python/server dependencies, database access and the shared machine-key path. A legacy `loadcoach` setting must be replaced by `front` for a front-owned gateway. |
| Address already in use | Turnstile uses 8443; the private gateway uses loopback 9443. Check `ss -ltnp`. |
| No tools for one account | Check group/account tool patterns, hidden tools, sensitivity and the selected tool set. Refresh the client's list. |
| No tools for any account | Configure at least one server; inspect the gateway journal for server startup/discovery failures. |
| Custom module omitted | Test its stdio command directly. Check its annotations, imports, duplicate names and any required setup section. |
| Files missing from a listing | Check the configured root, path permissions, dot paths and symlinks. The built-in tools omit private paths. |
| Remote server unavailable | Check its service, routing/firewall, bearer file and server URL. Missing servers are retried on tool listing, at most once every 30 seconds. |
