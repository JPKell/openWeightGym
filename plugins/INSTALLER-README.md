# openWeightGym @PLUGIN@ adapter installer — @INSTANCE@

This archive installs the openWeightGym HTTP adapter for the **@PLUGIN@ / @INSTANCE@**
instance. It includes a private pairing key: keep the archive and extracted files
private. It does **not** install Hermes Agent or configure its models, credentials,
or profiles. Hermes must already work under the account running the adapter.

## Requirements

- Linux with Python 3.11 or newer (`python3 --version`).
- Python virtual-environment support, including pip/ensurepip.
- Access to Python package downloads, or an appropriately configured pip cache/index.
- A working systemd user session for the default service installation. Use
  `--no-service` for a foreground installation without systemd.

Run the installer as your normal login account. Using `sudo` would select root's
home directory and user service rather than yours.

## Install

Extract the downloaded archive, open a terminal in `suite-@PLUGIN@-@INSTANCE@`,
and run:

```bash
./install.sh
```

The default installation directory is
`$HOME/.local/share/suite-adapters/@PLUGIN@/@INSTANCE@`. The service is
`suite-adapter-@PLUGIN@-@INSTANCE@.service`.

Other options:

```bash
./install.sh --help
./install.sh --directory "$HOME/adapters/@PLUGIN@-@INSTANCE@"
./install.sh --no-service
```

The installer copies the adapter and database modules, creates its `.venv`, installs
Python dependencies, and, by default, enables and starts its systemd user service.
`settings.json` and `keys/turnstile.key` are created only if absent. Reinstalling preserves
those files and `data/runtime.db` (session mappings and request receipts).

The adapter's `.venv` is separate from Hermes's existing environment. In openWeightGym's
Hermes instance settings, configure the **Hermes** `venv` and `repository`, or its
`cli` executable. Paths must exist on this adapter machine and be accessible to
this account.

## Verify and connect

```bash
systemctl --user status suite-adapter-@PLUGIN@-@INSTANCE@.service --no-pager
journalctl --user -u suite-adapter-@PLUGIN@-@INSTANCE@.service -n 50 --no-pager
```

In openWeightGym, open **System → Plugins**, synchronize the instance settings, discover
profiles/models, and enable the desired public aliases. The instance URL must reach
the configured `listen_host` and `listen_port`. Hermes defaults to `127.0.0.1:8450`:
a loopback listener is reachable only on the adapter machine. For another front
host, configure an appropriate reachable listener and private network access.

If installed with `--no-service`, run in the foreground:

```bash
cd "$HOME/.local/share/suite-adapters/@PLUGIN@/@INSTANCE@"
./.venv/bin/python -m plugins._runtime --config settings.json
```

Use your chosen directory instead if you supplied `--directory`. Stop foreground
execution with Ctrl+C. Avoid starting a second adapter against the same settings
while its service is already running.

## Recover from an installation error

An interrupted installation can be retried in place. Download a fresh archive for
the **same instance**, extract it separately, and run its `./install.sh` again with
the same `--directory` option, if any. Do not delete the installed directory:
it contains your pairing key, settings, and session/receipt database.

Reinstallation does not restart an already running service. When ready to interrupt
its active requests and load updated code, run:

```bash
systemctl --user restart suite-adapter-@PLUGIN@-@INSTANCE@.service
```

| Error or symptom | What to check or do |
|---|---|
| `error: package directory 'db' does not exist` during wheel requirements | This was an installer packaging defect, not a pip configuration error. Get a regenerated bundle from an updated openWeightGym front and rerun it. The corrected installer copies `db` alongside `plugins`. |
| `ModuleNotFoundError` for `db.connection`, `db.migrations`, or `db.schemas` at startup | Get the corrected bundle and reinstall; these runtime modules must be included as well. |
| `python3: not found`, unsupported Python, or venv/ensurepip failure | Install a supported Python with your distribution's virtual-environment support, then retry. |
| pip download/build fails | Read the first error above the final `CalledProcessError`. Check package-index/network access, Python version, and write access to the chosen directory. |
| `Failed to connect to bus` from `systemctl --user` | Run in the intended user's systemd login session, or install with `--no-service` and use the foreground command. The Python installation may already have completed. |
| Service fails or port is already in use | Read its journal and check the configured listener port and other adapter processes. |
| Hermes executable/module/profile cannot be found | Check the Hermes `cli`, `venv`, `repository`, and profile settings on this machine. The adapter environment does not contain Hermes. |
| openWeightGym cannot reach the adapter | Check service status, listener address/port, front's instance URL, and network access. Restart the adapter locally after listener changes. |
| Pairing/authentication rejected | Confirm the archive belongs to this exact instance. Reinstallation preserves an existing key and settings; a new archive does not overwrite them. Do not publish either key while investigating a mismatch. |

For help, retain the first install error and relevant service journal lines. Remove
credentials and private settings before sharing them. Never share `turnstile.key`.

Adapter configuration, sessions, and gateway controls are described in
[the plugin README](plugins/@PLUGIN@/README.md). Hermes gateway controls in openWeightGym
operate the gateway; they do not restart this adapter's systemd service.

The bundle includes the minimal shared `loadcoach` tracing/signature package. Optional spans
use the existing runtime database and are collected by openWeightGym's existing controller. Preserve
`settings.json`, pairing keys and `data/` when updating an installed instance. Older runtimes
remain callable but cannot provide the new distributed timeline.
