# Adding tools to the ToolLocker

The ToolLocker gateway runs on front, with file/code tools on their owning machines.
Its existing public MCP address is `http://10.77.10.42:8443/mcp`. A harness, such as
Hermes, connects to it with an API key and is offered the tools its account may use. This page
says how to add a tool of your own. How the ToolLocker itself works is in
[docs/loadcoach.md, section 13](../docs/loadcoach.md#13-the-toollocker).
The [local application sweep](../docs/loadcoach.md#application-sweep-2026-10-20) records pending
discovery, transport and file-read limits; each tool's docstring describes its callable contract.

## Where tools live

Tools live in a **tool folder**. Each `.py` file in it holds one tool or several. When the tool
host starts, it reads every file, registers every tool it finds, and serves them all:

```bash
python -m toollocker.tools FOLDER --settings tools.toml                                    # for a ToolLocker on this machine
python -m toollocker.tools FOLDER --settings tools.toml --http 9100 --key-file host.key --bind 10.77.10.42
```

On a machine set up with `scripts/install-tool-host.sh`, the tool folder is
`~/.local/share/toollocker-host/tools`, the settings are `~/.config/toollocker-host/tools.toml`,
and the service is `toollocker-host.service`. The suite's own `files.py` and `code.py` are
already there.
The installer accepts `--bind ADDRESS` to listen on one IP address; use the private interface
that compute can reach. Its user service has a 512 MiB memory ceiling, 32-task limit and no new
privileges.

## Writing a tool

A tool is a Python function marked `@tool`:

```python
"""Notes: find and read the notes in one folder."""

from pathlib import Path

from toollocker.tools import tool

FOLDER = Path()  # set by setup


def setup(settings: dict) -> None:
    """Called once at start with this file's section of tools.toml, here [notes]."""
    global FOLDER
    FOLDER = Path(settings["folder"]).expanduser()
    if not FOLDER.is_dir():
        raise ValueError(f"notes: {FOLDER} is not a folder")


@tool
def find_notes(word: str, most: int = 20) -> str:
    """Find notes whose text contains a word. Returns one file name per line, at most `most`."""
    found = [path.name for path in sorted(FOLDER.glob("*.md")) if word.lower() in path.read_text(errors="replace").lower()]
    return "\n".join(found[:most]) or "No note mentions that."


@tool
def read_note(name: str) -> str:
    """Read one note by the file name find_notes gave."""
    path = FOLDER / name
    if path.parent != FOLDER or not path.is_file():
        raise ValueError(f"There is no note named {name}.")
    return path.read_text(errors="replace")
```

The rules:

- **`@tool` marks what is offered.** Other functions in the file, such as helpers, are not.
- **The function's name is the tool's name.** Use lowercase words joined by `_`. Each name must
  be unique in the folder: two files with a tool of the same name stop the host at start.
- **The docstring is what the model reads** to decide when to use the tool and how. Write it
  for the model: what it does, what it returns, what each parameter means.
- **Parameters are `str`, `int` or `bool`.** A parameter without a default is required. Any
  other kind leaves the whole file out at start, with the reason in the host's output.
- **Return text.** A result longer than 40,000 characters is cut, and says so.
- **To refuse, raise `ValueError`** with a reason the model can act on. The model sees the
  message. Any other error is reported as a fault in the tool.
- **`setup(settings)` is optional.** A file with one is handed its own section of the settings,
  `[notes]` for `notes.py`, and is left out if that section is missing. A file without one is
  always loaded. `setup` should raise `ValueError` when the settings are wrong.
- **One call runs at a time** on one tool host. A slow tool holds up the next call.

The settings for the example:

```toml
[notes]
folder = "~/notes"
```

### What a tool can do

A tool runs as the account that runs the tool host, with everything that account may do. The
ToolLocker checks who may *call* a tool; it cannot check what the tool does once called. So:

- Keep a tool to its job. `files` and `code` only read, and refuse anything outside the folders
  they are given, hidden files, and files only their owner may read.
- Never pass text from the model to a shell, or to a program as an option. `code` passes a
  search pattern after `-e` and a path after `--` for this reason.
- A tool that changes things, such as sending email, should get a level of `sensitive` or
  `restricted` on compute, so that only groups cleared for it see it and every call is kept.

## Adding a tool on a remote tool host

1. Put the file in `~/.local/share/toollocker-host/tools/`. Do not edit `files.py` or
   `code.py` there: they are replaced when the installer runs again. Copy one to a new name
   instead.
2. If it has a `setup`, add its section to `~/.config/toollocker-host/tools.toml`.
3. Check it on its own, before compute sees it:

   ```bash
   cd ~/.local/share/toollocker-host
   printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
     | python3 -m toollocker.tools tools --settings ~/.config/toollocker-host/tools.toml
   ```

   It prints which files it read and which tools each holds, then the tool list as JSON. A file
   it left out is named with the reason.
4. Restart the host: `systemctl --user restart toollocker-host.service`. Then
   `journalctl --user -u toollocker-host.service` shows the same lines.

5. On the gateway host (front in this installation), restart the ToolLocker: `systemctl --user restart toollocker.service`. It reads
   each server's tools when it starts, so a tool added to a host is offered only from then.

## Adding a tool folder on compute

Edit the gateway's `~/weightroom/settings/toollocker.toml` on front and restart its
ToolLocker. A folder on compute runs there through fixed SSH stdio:

```toml
[servers.mine]
command = ["/usr/bin/ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "10.77.10.84", "/home/jpk/ai/suite/.venv/bin/python", "-m", "toollocker.tools", "/home/jpk/tools", "--settings", "/home/jpk/tools/tools.toml"]
description = "my own tools on compute"
```

A folder on front can use a direct `command` with front's Python executable,
`/home/jpk/weightroom/.venv/bin/python`, and its own local paths. SSH commands are
fixed operator configuration; tool arguments travel through MCP over stdin.

Any other MCP server can be put behind the ToolLocker the same way: by `command` if it speaks
stdio, or by `url` and a private `key_file` if it answers over HTTP. For an intentionally public,
unauthenticated HTTPS server, use `url` with `public = true` instead of `key_file`; this explicit
setting never sends an Authorization header. Review its tools and set server/tool levels and
group/account access before offering them.

## After a tool appears

A tool is offered as the server's name, two underscores and its own name, such as
`nas__find_notes`. The first time the ToolLocker sees it, and whenever its description changes,
it writes that to the event log. Then, on the site:

- **System → Tools** lists it under recorded tool descriptions. Set its level there if it should
  differ from its server's, and add it to tool sets. **Try a tool** calls it as you.
- **A group's page** sets which tools its members are shown, by name or pattern such as `nas__*`.
  A group set to every tool sees a new tool as soon as it is offered, within its level.
- **An account's page** shows it to, or hides it from, one account.

A harness that listed tools before the tool existed sees it the next time it lists them.

## This installation's remote qualification

On 2026-10-16, the front machine's private-interface host served a harmless `files` folder
to compute; both tools were discovered through the live ToolLocker. The owner-only bearer
file, group/account/tool-level restrictions, request selection, description, signed caller
identity and sensitivity were checked. Missing/wrong keys, browser Origin, outside-folder
reads, hidden tools and a revoked account were refused. A public Microsoft Learn read-only
HTTP MCP server was separately initialized, discovered and called through an isolated
ToolLocker/front helper. [Exact bounded evidence](../data/qualification/delivery-2026-10-16.json)
and [host installation details](../docs/installation.md#remote-tool-hosts).

Front relocation, fixed SSH stdio commands, preserved tool identities, recovery boundaries and
verification are maintained in [the installation guide](../docs/installation.md#front-toollocker-relocation-milestone-128).
