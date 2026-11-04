# ToolLocker setup and adding tools

ToolLocker is the MCP gateway for tools such as reading files, searching code and calling your
own Python functions. A client connects to `http://FRONT:8443/mcp` with an openWeightGym API key.
Turnstile checks the account and signs the request for the private gateway on port 9443.
Tools run on the machine that owns their files or service; the gateway can run on front while
compute is off. An MCP-capable client or agent executes tool calls; **Settings → Tools → Try a
tool** checks them independently of a model.

These same instructions are available in the console at **Docs → ToolLocker** (`/docs/toollocker`).

## Why tools can be missing

The current **Console only** installer selection installs WeightRoom and Turnstile, and optionally
the voice gateway. It does not install or configure the ToolLocker service. Repairing that same
selection does not add it. A configured GPU node may have a gateway of its own, but front-local
tools need the front setup below to work without that node.

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

Create an allowed folder:

```bash
mkdir -p data/shared
printf '%s\n' 'ToolLocker is working.' > data/shared/example.txt
chmod 644 data/shared/example.txt
```

Create `settings/toollocker.toml` if absent. If it exists, merge the settings and preserve its
server names, levels and tool sets. Replace `/home/you/openWeightGym` below with the absolute
path printed by `pwd`; TOML command arguments do not expand `~` or `$HOME`.

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
```

Use `front`, omitting `loadcoach`: this shares the console's existing SQLite or PostgreSQL accounts
store and signing key. The included file tools skip hidden paths, paths without the other-read
permission bit, and symlinks escaping the allowed folder. Keep the allowed folder separate from
settings, keys and databases.

### 3. Route MCP to the front gateway

In `settings/weightroom.toml`, add or update these keys **inside the existing `[turnstile]` table**:

```toml
[turnstile]
front_settings = true
toollocker_url = "http://127.0.0.1:9443"
```

Keep the rest of that table, including its machine key and ports. Ensure its existing `ports`
list includes `8443`; preserve any voice and other configured ports. Avoid adding a second
`[turnstile]` table. Turnstile continues to own 8443; ToolLocker must use 9443.

### 4. Install the missing service

For a missing unit, run this from the checkout. If a unit already exists, retain its custom
settings and verify its `WorkingDirectory` and `ExecStart` instead of replacing it.

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/toollocker.service <<EOF
[Unit]
Description=ToolLocker (front MCP gateway)
After=network-online.target

[Service]
WorkingDirectory=$PWD
ExecStart=$PWD/.venv/bin/toollocker --config $PWD/settings/toollocker.toml
Restart=on-failure
RestartSec=3
MemoryMax=2G

[Install]
WantedBy=default.target
Alias=tooldoor.service
EOF
systemctl --user daemon-reload
systemctl --user enable --now toollocker.service
```

Then restart Turnstile and WeightRoom to pick up routing and front-owned settings:

```bash
systemctl --user restart turnstile.service weightroom.service
systemctl --user is-active toollocker.service turnstile.service weightroom.service
journalctl --user -u toollocker.service -n 50 --no-pager
```

Restart after active requests finish: restarting Turnstile interrupts requests it is carrying.
Installing just the gateway does not require starting a compute node. The standalone
`toollocker-host.service` is optional when a local stdio command runs the tools.

## Verify the connection

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
| Unit not found | Create the front gateway config and service above; the console-only preset omits it. |
| Tool service has not reported configuration | Check `systemctl --user status toollocker` and its journal, then `front_settings`, `toollocker_url` and port 8443 in the console configuration. |
| Gateway will not start | Check absolute command paths, Python/server dependencies, database access and the shared machine-key path. A legacy `loadcoach` setting must be replaced by `front` for a front-owned gateway. |
| Address already in use | Turnstile uses 8443; the private gateway uses loopback 9443. Check `ss -ltnp`. |
| No tools for one account | Check group/account tool patterns, hidden tools, sensitivity and the selected tool set. Refresh the client's list. |
| No tools for any account | Configure at least one server; inspect the gateway journal for server startup/discovery failures. |
| Custom module omitted | Test its stdio command directly. Check its annotations, imports, duplicate names and any required setup section. |
| Files missing from a listing | Check the configured root, path permissions, dot paths and symlinks. The built-in tools omit private paths. |
| Remote server unavailable | Check its service, routing/firewall, bearer file and server URL. Missing servers are retried on tool listing, at most once every 30 seconds. |
