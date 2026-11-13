# Installation

openWeightGym installs with one command. It checks the machine, asks a few questions (Enter takes
the defaults), downloads what is needed, starts everything and checks that it answers.

```bash
curl -fsSL https://raw.githubusercontent.com/JPKell/openWeightGym/main/install.sh | bash
```

From a checkout, `./install.sh` does the same and the checkout becomes the installation. Running
the installer again offers **Update**, **Repair**, **Install components**, **Add a compute node**
(on a console) and **Uninstall**.

A new console installation asks whether to enable image generation, speech (TTS, STT and Voice),
music/audio generation, and model downloads. Change these later under **General → Platform services**;
changes apply immediately. Disabled services disappear from navigation, model-type pills and filters,
and their requests are refused. Hugging Face requires model downloads; Civitai requires both model
downloads and Images. Existing installations and unattended installs keep all four enabled by default.
For an unattended choice, use `--platform-services images,speech,audio,downloads`, a subset, or `none`.
Repair and updates preserve existing choices, settings and models.

Release-by-release delivery notes, runtime pins and recovery procedures for this project's own
deployment are in the [operations log](operations.md).

## What you need

| | |
|---|---|
| System | Linux on x86-64 or ARM64 with systemd: Ubuntu 22.04 or newer, Debian 12 or newer, Fedora, RHEL, Rocky and Alma 9 or newer, openSUSE Tumbleweed and Leap 16, Arch, and their relatives, or WSL2 with systemd turned on |
| Disk | About 3 GB, plus room for models |
| Tools | bash 4.4 or newer and curl. git, tar, gzip and awk are installed for you if missing (apt, dnf, zypper or pacman, told never to ask, since `curl \| bash` leaves nobody to answer) |
| Python | Nothing to do: the installer fetches Python 3.13 with [uv](https://docs.astral.sh/uv/) into the installation and leaves the system's Python alone |
| Graphics | Optional. NVIDIA cards need only the driver (the CUDA libraries come with the engine); AMD and Intel cards use Vulkan, or ROCm where it is installed; without a card, models run on the processor |
| Network | GitHub, Hugging Face (for the models you download afterwards) and, for other devices, the ports below |

In WSL, add `[boot]` and `systemd=true` to `/etc/wsl.conf`, run `wsl --shutdown` in Windows and
start WSL again before installing.

## What the installer does

1. **Warm-up.** Checks the system, systemd, the graphics cards (and the CUDA version the NVIDIA
   driver supports), disk space, and whether this account already runs openWeightGym from another
   folder, in which case it stops rather than replace those services.
2. **Game plan.** Asks how this machine fits in (below), shows the plan and offers **Customize**:
   who it runs as, voice, the database, the llama.cpp build, power control and
   whether services keep running after you log out.
3. **Account.** Asks for your console user name and password, typed twice.
4. **Loading plates.** Fetches the code, uv and Python 3.13, a prebuilt llama.cpp for the cards it
   found, with progress bars.
5. **Setting up.** Runs [`scripts/install-suite.sh`](../scripts/install-suite.sh), which makes the
   Python environment, writes settings for this machine's part, makes the keys and the `weightroom`
   service account, and installs and starts the systemd services.
6. **Spotting.** Checks every service is running and answering.

It ends with the console's address, your sign-in, the API address and an API key (shown once).
No model comes with it: download one under **Models → HuggingFace** in the console, then add it
as a model from **Files**.
Step output is in the private log it names, under `~/.local/state/openweightgym/`; command
arguments containing credentials are omitted. Logs are readable only by their owner.

## One machine or several

| Choice | Runs | Use it for |
|---|---|---|
| **Everything on this machine** | the console, the API gateway, the model runner, tools and the monitor | one box with a graphics card: the quickest start |
| **Console only** | the web console, the API gateway and ToolLocker | a small always-on machine; GPU machines join it |
| **GPU node** | the model runner and the monitor | a machine whose cards a console should use |

Ports, for a firewall:

| | Console machine | GPU node |
|---|---|---|
| Web console | 8440 | |
| API (OpenAI-compatible) | 8442 | 8442, reached by the console only |
| Tools (MCP) | 8443, through the front gateway | |
| Monitor | 8444, through the gateway | 8444, reached by the console only |
| Voice | 8445 to 8447, when installed | 8445, when installed |

On one machine the gateway keeps 8442 to 8445 and the programs behind it listen on loopback only,
1000 above (9442 to 9445); `compute = "http://127.0.0.1:9442"` in `settings/weightroom.toml` tells the
gateway so. Other devices reach the console on 8440 and the API on 8442.
ToolLocker always listens privately on `127.0.0.1:9443` on front, including a console-only
installation. Turnstile authenticates public MCP requests on 8443 and routes them locally;
the gateway shares front's accounts/event store and remains available without compute.

Fedora, RHEL and openSUSE turn their firewall (firewalld) on as they install, and ufw may be on
elsewhere. The installer does not open ports itself: it checks them, and when the firewall keeps
other machines out it says so at the end, with the command that opens them, such as
`sudo firewall-cmd --permanent --add-port=8442/tcp && sudo firewall-cmd --reload` or
`sudo ufw allow 8442/tcp`. `owg doctor` checks the same. A GPU node whose port 8442 is closed
installs and runs, but the console cannot reach it. With Spotter installed, allow its TCP port
8444 from the console machine too: telemetry and model-file transfers use that port. A node
can appear ready on 8442 while model transfers fail at zero bytes because 8444 is blocked.
Both installers and `owg doctor` read the selected services' configured ports, including custom
ports, gateway listeners and optional voice listeners. Loopback-only listeners are excluded.
Checks report allowed ports, missing rules, inactive firewalls or rules they cannot read, and
print UFW or firewalld inspection and repair commands. These inspect local rules; remote network
firewalls still need their own checks.

## Docker qualification for image-model installation

An isolated console, Turnstile, two LoadCoaches and two Spotter file receivers are defined in
`tests/docker/images/compose.yaml`. Each node has its own volume; generated accounts and small
image-header fixtures are separate from the installed suite. Only the console is published,
on loopback port 18440 (`OWG_IMAGE_TEST_PORT` changes it). The native executable is a fixture:
this deployment tests model installation, not image generation or GPU qualification.

```bash
docker compose -f tests/docker/images/compose.yaml build seed
docker compose -f tests/docker/images/compose.yaml up -d --no-build
```

The browser/API qualification creates its own disposable Compose project and removes its
containers and volumes afterwards. With the development dependencies and Playwright Chromium
installed, run:

```bash
OWG_IMAGE_DOCKER=1 .venv/bin/pytest tests/test_image_install_docker.py -n 0 -q
```

It tests single-node and simultaneous multi-node creation, storage promotion, every selected
component's SHA-256 and relocated settings, concurrent installs sharing files, failed preflight
with no partial model creation, and a killed gateway followed by a browser retry of its saved
request. See [installation recovery](operations.md#model-installation-progress-and-restart-recovery-2026-11-12).
To remove a manually started qualification deployment:

```bash
docker compose -f tests/docker/images/compose.yaml down -v
```

Installed services continue to use `owg update; owg restart`.

## Docker qualification with real models

`tests/docker/qualification/compose.yaml` runs a disposable front and three peer nodes
(alpha, beta and gamma), each with a separate Docker volume and generated credentials.
The console is published on `127.0.0.1:19440`; API and MCP diagnostic pages use loopback
ports 19442 and 19443. It never reads an installed suite's
settings, accounts, model folders or databases. Downloads consume real storage; choose
small models and account for the front's storage/cache copies and each destination copy.

```bash
docker compose -f tests/docker/qualification/compose.yaml build seed
docker compose -f tests/docker/qualification/compose.yaml up -d --no-build
docker compose -f tests/docker/qualification/compose.yaml exec alpha bash scripts/install-llamacpp.sh --prebuilt --backend cpu --prefix /opt/owg-runtimes/llamacpp --yes
.venv/bin/python tests/docker/qualification/browser.py --output /tmp/owg-browser-review
.venv/bin/python tests/docker/qualification/scenarios.py download-llm
.venv/bin/python tests/docker/qualification/scenarios.py install-llm --node beta
```

The browser runner discovers actual navigation links, waits for deferred content, and
records screenshots, status codes, browser errors and page overflow at desktop and phone
widths. Scenario checks perform real downloads, installations, routing, media generation and
downloads, transcription, backups and tool calls through browser controls. For example,
`scenarios.py generate-image --name MODEL`, `speech --name MODEL`,
`transcribe --name MODEL --file /tmp/owg-qualification-scenarios/generated-speech.wav`,
`routing --name MODEL`, `backups`, and `tools` use the installed qualification models.
`gpu.compose.yaml` optionally grants alpha NVIDIA devices and read-only driver/toolkit
access; inspect its host paths before using it. Install the CUDA runtime inside alpha at
`/opt/owg-runtimes/llamacpp-gpu` with the same installer before running GPU models.
Runtime installations and model downloads are confined to the containers and named volumes.
Each worker has a private cgroup namespace, a 16 GiB ceiling and no swap. `SYS_ADMIN` is used
only to delegate that container's cgroup; startup refuses an unbounded or non-private mount.
No host cgroup mount or installed service policy is changed.

Optional native media engines and CPU voice bundles are installed inside the same runtime volume:

```bash
docker compose -f tests/docker/qualification/compose.yaml exec beta python scripts/install-sdcpp.py --backend cpu --prefix /opt/owg-runtimes/sdcpp --jobs 8
docker compose -f tests/docker/qualification/compose.yaml exec beta python -m soundroom.install --root /app --backend cpu --prefix /opt/owg-runtimes/audiocpp --jobs 8
docker compose -f tests/docker/qualification/compose.yaml exec beta python scripts/voice-models.py /opt/owg-runtimes/voice
```

The optional `postgres` profile runs an unexposed PostgreSQL 16 container. After restoring an
accounts store into it and writing the owner-only `/state/front/qualification-postgres.dsn`,
`postgres.compose.yaml` temporarily binds front's accounts to that store. The normal Compose
file restores SQLite bindings; its original files remain intact. The image includes matching
PostgreSQL 16 recovery tools as well as the distribution's client tools.

The optional `wake.compose.yaml` configures gamma's wake address to a receiver on the
isolated Docker network. After installing a CPU text model named `qualification-multi` on
beta and gamma with one request at once and a 2,048-token context, run
`scenarios.py wake --name qualification-multi`. The browser driver stops only this project's
gamma container, verifies that passive pages send no wake, fills beta, receives the real magic
packet, and starts gamma to emulate power-on. The receiver has no Docker socket access;
this checks packet delivery and request recovery, not physical hardware wake.

Remove only this qualification environment when finished:

```bash
docker compose -f tests/docker/qualification/compose.yaml -f tests/docker/qualification/gpu.compose.yaml -f tests/docker/qualification/wake.compose.yaml down -v
```

## ToolLocker on the console machine

New **Console only** and **Everything on this machine** installations include ToolLocker with
the bundled file and code tools. **GPU node** installs omit the gateway; remote tool servers
can still run there and connect to front. Both installers use this ownership in their presets.
Voice remains optional in `install.sh`; the lower-level `scripts/install-suite.sh` presets also
select the corresponding voice services before its editable checklist.

Existing settings and the service list saved for Repair are preserved. Older console-only
installs need the [front ToolLocker setup](../toollocker/README.md#install-toollocker-on-front)
to add it; existing compute gateways are not automatically moved or removed. The same guide,
available at **Docs → Modules → ToolLocker**, covers verification, adding tools and remote tool hosts.

## Adding a GPU node

A GPU node joins a console with a one-time code; no keys are copied by hand.

1. On the console, run `owg pair`, or open **Settings → Compute nodes** and press **Make a
   pairing code** (superusers only). Either shows a command like:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/JPKell/openWeightGym/main/install.sh | bash -s -- --join http://192.168.1.10:8440 --code ABCD-EF23
   ```

2. Run that command on the GPU machine. Its installer uses the code as soon as you confirm the
   plan, before downloading anything.

The console records the node at the address its request came from: as the main compute node when
it has none yet, otherwise as a further node with a key of its own (`[machines.NAME]` in
`settings/weightroom.toml`, with `keys/machines/NAME.key`). It keeps a private copy of its settings as they were
(`weightroom.toml.before-pairing-…`) and restarts the gateway and the console to use the node.
A code works once, for ten minutes; ten wrong codes from one address in ten minutes are refused.

Without a code, choose **GPU node** and **With the console's key**: give the console's address and
paste its `keys/machine.key`. Then add `compute = "http://NODE:8442"` to the console's
`settings/weightroom.toml` and run `owg restart turnstile weightroom` there.

Add `--voice` to the node's install command to receive a separate voice-service credential and
enable the console's voice API route. Install the console with `--voice` for the browser voice
gateway. For manual pairing, also copy the console's `keys/voice-service.key` and pass
`--voice-key-file FILE`; the machine signing key cannot substitute for it. Voice setup creates
the service and browser accounts and verifies the speech bundles required by the settings. Once
you have added a model, choose it as the conversation model under **Settings → Voice**.

## For you, or for the whole machine

By default openWeightGym is installed **for you**: in `~/openWeightGym`, as your own systemd user
services. Linger is turned on so they run from boot whether or not you are signed in (Customize
can leave it off).

**For the whole machine** (`--system`, or Customize → *The whole machine*) it is run by its own
`openweightgym` account, which starts at boot, and its files go in the usual places:

| | For you | For the whole machine |
|---|---|---|
| Code, Python, engines | `~/openWeightGym` | `/opt/openweightgym` |
| Settings | `~/openWeightGym/settings` | `/opt/openweightgym/settings`, shown as `/etc/openweightgym/settings` |
| Keys | `~/openWeightGym/keys` | `/etc/openweightgym/keys` |
| Data | `~/openWeightGym/data` | `/var/lib/openweightgym/data` |
| Models | `~/openWeightGym/models` | `/var/lib/openweightgym/models` |
| Services | `~/.config/systemd/user` | the account's own, in `/var/lib/openweightgym/.config/systemd/user` |
| `owg` | `~/.local/bin/owg` | `/usr/local/bin/owg` |

A system installation needs sudo while installing, and `owg` asks for it when it acts for the
account. Its services are the account's own systemd user services, so every part of the console
that starts, stops or reads services works the same as in an installation for you. It uses SQLite;
move it to PostgreSQL afterwards with the [database guide](database-storage.md). The account is
added to the `video` and `render` groups for the graphics cards.

With `--database postgresql` (installations for you), PostgreSQL is installed and its database
cluster made the way each distribution expects: Debian and Ubuntu make one with the package,
Fedora and RHEL with `postgresql-setup --initdb`, Arch and openSUSE with `initdb` in
`/var/lib/postgres/data` and `/var/lib/pgsql/data`. A new cluster allows peer logins for the
`postgres` account and password (SCRAM) logins over TCP, which is how openWeightGym signs in; the
installer checks that sign-in works before going on, and if an existing server refuses it, shows
the `pg_hba.conf` lines to add.

## Looking after it: `owg`

| Command | Does |
|---|---|
| `owg` | shows what is running, then a menu: restart services (choosing which), restart another machine's services over SSH, update, repair, install components, doctor, logs, add a compute node, uninstall |
| `owg status` | programs, addresses, the llama.cpp build and the models' size |
| `owg start`, `stop`, `restart [PROGRAM…]` | controls the services, all of them by default |
| `owg restart --host USER@HOST [PROGRAM…]` | restarts services over SSH using that host's OWG installation; all installed services by default |
| `owg logs [PROGRAM] [-f] [-n LINES]` | the latest from the programs' journals (80 lines unless `-n` says otherwise); `-f` follows them |
| `owg update [--yes] [--plain]` | fetches the latest code, rebuilds the console's Voice desktop ZIP and selects this checkout ahead of older runtime source overrides, including when an installation is unfinished |
| `owg repair [--yes] [--plain]` | reinstalls packages and services as they are |
| `owg install [OPTIONS]` | adds omitted services and model engines; equivalent to `install.sh --components` |
| `owg doctor` | checks the environment, services, settings, llama.cpp, key permissions, linger, the console and disk space, and says what to fix |
| `owg pair [--minutes N]` | makes a pairing code for a GPU node (on a console), good for N minutes |
| `owg uninstall [--yes] [--everything] [--no-backup] [--plain]` | removes openWeightGym, choosing what to keep; `--yes` keeps settings, keys, data and models without asking, `--everything` removes them too after a backup, which `--no-backup` skips |
| `owg version`, `owg help` | the installed version and commit; every command and option |

Before any command, `--plain` turns off colour and animation and `--yes` takes every default
without asking.

Update pulls code and puts this checkout first in existing service `PYTHONPATH` overrides,
preserving custom library paths and other service settings. It does not install packages, build
engines, restart services or resume an unfinished installation. Run `owg restart` when ready;
start/restart also selects the checkout, including the first restart after an older updater fetched
this repair. Changed override files have private rollback copies and a path manifest under the
service owner's `$XDG_STATE_HOME/openweightgym/runtime-source-updates/` (normally
`~/.local/state/openweightgym/runtime-source-updates/`). An unsupported override of the service
executable stops the command with an error instead of claiming the new code will run.
A saved installation plan remains available by running `install.sh` separately.
Updates refuse to run over changes of your own to the code; commit or stash them first. Settings
files are kept; enabling voice adds its missing gateway port and pairing updates node addresses,
with private backups before those changes. Replaced service units are backed up beside their
originals as `NAME.service.before-suite-install-TIME`, including local edits to generated units.
Service control commands return a nonzero status if any requested service fails.
Components versions are recorded after installs, code updates and successful starts/restarts.
Spotter on each compute node and Turnstile on Front recheck them independently of page visits.
The **Component version check interval** setting is in minutes (60 by default, above zero);
changes apply within a minute. Configure it per node under **Compute nodes**, and for Front
under **Settings**. In files it is `component_versions_every` in `settings/spotter.toml` on
compute, or `[turnstile]` in `settings/weightroom.toml` on Front. The Components page refreshes
its display every 30 seconds, reading saved versions without waking sleeping nodes or running
model workers. Engine version probes use bounded `--version` calls; Strata uses its source revision.

Remote restarts need working SSH key authentication and `owg` installed on the destination.
For example, `owg restart --host jpk@compute loadcoach spotter` restarts those two services.
The remote installation handles its service account and any sudo prompt in an interactive terminal.

## The engine: llama.cpp

The installer runs [`scripts/install-llamacpp.sh`](../scripts/install-llamacpp.sh), which installs
the official ggml-org build pinned to `b12008` (llama.cpp `344846b4`) for the cards it finds:
CUDA 13.4 or 12.8 on NVIDIA (by what the driver supports), ROCm on AMD with ROCm installed, Vulkan on
other AMD and Intel cards, and the processor build otherwise. Missing system libraries, such as
`libgomp`, are named, with the package that provides them. Run it again at any time:

```bash
scripts/install-llamacpp.sh                      # choose a download or build from source
scripts/install-llamacpp.sh --check              # the build in use
scripts/install-llamacpp.sh --prebuilt --backend vulkan  # another prebuilt kind
scripts/install-llamacpp.sh --from-source        # install build requirements and compile here
OWG_LLAMACPP_BUILD=b12024 scripts/install-llamacpp.sh   # another release
```

Choose **Build here from source** to retry a build skipped during installation. The installer adds
missing C/C++ build tools, CMake, make, pkg-config and OpenSSL development headers with the system
package manager. It also installs the selected backend's requirements: the CUDA toolkit (including
`nvcc`, runtime and cuBLAS development files), Vulkan headers and shader compiler, or HIP and BLAS
libraries for ROCm. It asks for your sudo password when needed and checks the requirements again
before compiling. GPU packages must be available in the configured repositories; failures name
the package command and, for CUDA, link to NVIDIA's repository instructions.

Existing toolkits are reused, including CUDA in `/opt/cuda`, `/usr/local/cuda` or the location named
by `CUDACXX`, `CUDAToolkit_ROOT`, `CUDA_PATH` or `CUDA_HOME`, without requiring a new login.
An interrupted build resumes from its existing source and build folders. Without a terminal, or
with `--yes`, a prebuilt download remains the default; use `--from-source` to select a source build
explicitly. Prebuilt downloads and `--check` do not install build tools.

Builds are kept side by side in `runtimes/llamacpp/releases`, and `runtimes/llamacpp/current` points
at the one in use, which is what LoadCoach's `llama_server` setting names. Nothing is restarted:
models loaded from then on use the new build. To use a llama-server you already have, choose
**One I already have** in Customize, or pass `--llama /path/to/llama-server`.

## Adding components and optional engines

If an installation failed, choose **Discard failed install** in `owg`, or run
`owg discard-install`. It previews unfinished engine build folders and asks before deleting
them, then cancels the entire saved retry plan. Completed engines and services, settings,
keys, models, shared Python environments and installed build tools (including CUDA) are kept.
External or symlinked runtime folders are left alone. A private copy of the old receipt is
kept as `.install-receipt.before-discard-*`. Cancelled choices are not resumed on the next run;
use **Install components** to select anything you want to try again. `--yes` skips confirmation.

The node installer offers **Strata**, **stable-diffusion.cpp** (images) and **audio.cpp**
(music, sounds and speech). They are optional; unattended installs keep them unchanged unless
selected explicitly. Image and audio builds offer CUDA, Vulkan or CPU; Strata uses CUDA.
All source builders share llama.cpp's toolchain setup: missing git, cmake, C/C++ compilers,
development headers and the selected CUDA or Vulkan toolkit are installed with the system
package manager. Existing CUDA toolkits outside `PATH` are reused, including `/opt/cuda`,
`/usr/local/cuda` and locations named by `CUDACXX`, `CUDAToolkit_ROOT`, `CUDA_PATH` or `CUDA_HOME`.
Build tools and GPU toolkits remain installed after builds, failures and uninstallation;
openWeightGym does not purge them. Model weights are separate.

Run **owg → Install components**, or `owg install`, to add services omitted earlier, including
voice, monitoring and tool services appropriate to this machine's role, or any of the engines
(including llama.cpp). Existing services stay selected internally; their settings, unit
customizations and data are kept. Selected services restart after installation. An interrupted
addition saves its selection: rerun the installer to continue.

```bash
owg install --yes --sdcpp install --sdcpp-backend cuda --audiocpp install
owg install --yes --strata install --strata-host-ram-mib 22528
owg install --yes --services spotter
owg install --yes --llama auto
```

These engine flags also work with `install.sh` during initial setup and with
`scripts/install-suite.sh`. `skip` and `keep` leave existing engines installed.
Adding voice to a remote node needs the console's voice-service key: the installer can request
a new pairing code, or accept `--voice-key-file FILE` (and `--services voice`).

Strata builds the [reviewed source revision](https://github.com/Niko1221/Strata/tree/477ac13cbe8e582c20484871ce459d20d82df09d)
in `runtimes/strata`, with a private Python environment. It queries GPU compute capabilities through
`nvidia-smi` (7.5 or newer required) and passes explicit architectures so upstream can select
the matching FP4 kernels on RTX 50 cards. Selecting it permits hybrid host RAM use
up to `--strata-host-ram-mib` (22528 MiB by default). Its systemd drop-in delegates memory without
raising the worker's existing limits; the budget must fit within `MemoryMax`, with
`MemorySwapMax=0`. The adapter still requires supported text models and prepared tokenizer files.
Use `scripts/install-strata.sh --check` to verify the installed runtime without loading a model.

## Uninstalling

```bash
owg uninstall
```

or `./uninstall.sh` in the installation, or `curl -fsSL https://raw.githubusercontent.com/JPKell/openWeightGym/main/uninstall.sh | bash`.

It lists what the installer made, with sizes, as a checklist. The services, the program files,
the engines, `owg` and the keep-running setting are ticked; settings and keys, data, models and
PostgreSQL databases are not, so by default they stay and a later install in the same place picks
them up. Before anything you ticked of yours goes, it offers a backup:
`~/openWeightGym-backup-DATE-ID.tar.gz`, readable by you only, with settings, keys, data and PostgreSQL
dumps (not models). Data deleted without a backup needs `delete` typed to go ahead.

Only what the installer's receipt (`.install-receipt` in the installation) lists is removed, and never anything
outside the installation's own folders. An installation made before `install.sh` existed has no
receipt: the uninstaller points it out and leaves it alone.
The receipt holds public installation metadata, with no credentials; private data stays private.
Older `data/install-receipt` files are read and migrated during repair. An explicit `--dir` with
no receipt fails without selecting another installation. Metadata stays with files you keep so
a later install can recover them.

| Option | |
|---|---|
| `--yes` | remove the program and keep settings, keys, data and models, without asking |
| `--everything` | remove those too, after a backup |
| `--no-backup` | with `--everything`, skip the backup |
| `--dir PATH` | the installation to remove, when it is not found by itself |

## Unattended installs

Every question has an option, and `--yes` takes the defaults for the rest. With `curl | bash`, put
options after `bash -s --`.

```bash
# Everything on one machine, no questions
curl -fsSL …/install.sh | OWG_ADMIN_PASSWORD='…' bash -s -- --yes --topology all --admin me

# A console for the whole machine, then a GPU node that joins it
./install.sh --system --yes --topology front --admin me
./install.sh --yes --join http://192.168.1.10:8440 --code ABCD-EF23
```

| Option | |
|---|---|
| `--topology all\|front\|compute` | one machine, console only, or GPU node |
| `--system` | install for the whole machine |
| `--dir PATH` | where to install (also `OWG_HOME`) |
| `--components`, `--services LIST` | add omitted components; optionally name comma-separated services |
| `--discard-install` | remove partial engine builds and cancel a failed installation's saved choices |
| `--strata install\|skip\|keep`, `--strata-host-ram-mib N` | optional CUDA Strata runtime and host RAM budget |
| `--sdcpp install\|skip\|keep`, `--audiocpp install\|skip\|keep` | optional image and audio runtimes |
| `--sdcpp-backend cuda\|vulkan\|cpu`, `--audiocpp-backend cuda\|vulkan\|cpu` | explicitly select each native backend (default CUDA) |
| `--admin NAME` | the first account; its password from `OWG_ADMIN_PASSWORD`, or a temporary one is shown |
| `--join URL --code CODE` | join a console as a GPU node |
| `--front-url URL`, `--machine-key-file FILE` | join by hand instead |
| `--compute-url URL` | a console's GPU node, when it already exists |
| `--llama auto\|cuda-13.4\|cuda-12.8\|rocm\|vulkan\|cpu\|source\|skip\|PATH` | the engine |
| `--database sqlite\|postgresql` | PostgreSQL is installed and set up locally (installs for you only) |
| `--voice` | also speech recognition and voices |
| `--voice-key-file FILE` | voice service key the front keeps for a GPU node joined by hand |
| `--power-control` | let the console sleep, restart or switch off this machine (a polkit rule) |
| `--no-linger`, `--no-browser` | skip linger, or opening the console |
| `--ref BRANCH` | which branch to install |
| `--plain`, `NO_COLOR`, `OWG_QUIET=1` | no colour and no animation, or colour without animation |

## When something goes wrong

Start with `owg doctor`, then `owg logs PROGRAM`. The installer's own log is named at the end of
every run. Package and database-configuration steps save their output there and show the last
lines when they fail.

When something stops the installation, it says so in a red panel headed **FAILED**, with a
terminal bell: what went wrong, the last lines of the log, numbered steps that put it right (the
commands to type, for this distribution), the command that carries on where it stopped, and the
full log's path. Nothing finished is undone. When the failed step's own output makes the cause
plain, the panel says **Why** and leads with its fix: a full disk, a machine that cannot reach a
server (named), a proxy or certificate problem, another program holding the package manager,
signing keys or package lists out of date, a package missing from the system's sources, a missing
compiler or program, too little memory, a read-only system, a file owned by another account, sudo
that cannot ask for a password, or a port already taken. A mistyped option stops the same way,
before anything is touched. Problems that do not stop it, such as a service that did not start
(with the reason from its own journal), a closed firewall port or a llama.cpp that could not be
installed, are listed again in a yellow **Needs your
attention** panel at the end, each with its own steps, so none is lost in the scrollback.

An interrupted installation keeps its confirmed plan in `.install-receipt` with `state=pending`.
Run the installer again with the same `--dir` to choose **Continue previous install**, **Uninstall
components and start again**, or **Quit**. Continuing keeps existing settings, keys and passwords
and completes missing account passwords. Starting again opens the removal checklist and backup
prompts, then returns to setup with whatever you kept. Cancelling removal stops the restart.
Services stopped for a backup are restarted if removal is cancelled or the backup fails.
PostgreSQL removal failures stop the uninstall and preserve its connection files for recovery.
When local PostgreSQL connections were kept after database removal, provisioning restores their
managed roles and missing databases before compatibility checks. It verifies password authentication
and preserves existing database contents; connections owned by another role are left for manual repair.
When run through `curl | bash`, the uninstaller is fetched from the same source and branch as
the installer, so an older partial checkout cannot supply an incompatible recovery helper.
Continuing through `curl | bash` updates a clean Git checkout before setup; tracked local changes
are reported and left untouched. Repair uses the receipt's service list even when unit files are missing.
`--yes` or `--repair` continues the saved plan directly. The receipt becomes complete only after
the final service checks finish successfully. Passwords and consumed pairing codes are never stored in the receipt.

| Seen | What to do |
|---|---|
| *password authentication failed for user suite_…* | Update the checkout and continue the previous PostgreSQL install. Provisioning reconciles retained credentials before the compatibility check. |
| *systemd user services are not running* | in WSL, turn systemd on (above); in a container, install for the whole machine on the host instead |
| *openWeightGym is already installed for this account, in …* | update that installation with its own `install.sh`, or uninstall it first |
| *Another openWeightGym (or AiSuite) installation, not made by this installer …* | run the steps it lists: they stop the older services and move their unit files and drop-ins aside, into `~/.config/systemd/retired-units`; its folder and data are not touched |
| *Leftover systemd drop-ins would change what openWeightGym's services run* | move the drop-ins it lists aside, as it shows, and run the installer again |
| *PostgreSQL could not be installed with …* | run the package manager's refresh it names, check the package installs by hand, or install with `--database sqlite` |
| *PostgreSQL refuses the password sign-in openWeightGym uses* | add the `pg_hba.conf` lines it shows above the other `host` lines and reload PostgreSQL |
| Components says llama.cpp is missing after installation, with no engine steps in the log | Older installers could silently skip the engine when uv's `~/.local/bin/env` shadows the system program. Update the installer; repair the existing runtime with `cd ~/openWeightGym && scripts/install-llamacpp.sh --from-source`, which installs missing build requirements as described above. |
| *llama-server needs: libgomp.so.1* | let the installer add the package it names, or install it and run `scripts/install-llamacpp.sh` |
| *This prebuilt llama.cpp needs GLIBC_2.38, newer than this system's C library* | the prebuilt builds are made on a recent Ubuntu: install the build tools it names and run `scripts/install-llamacpp.sh --from-source` |
| *Firewall (ufw/firewalld) needs allow rules for required TCP ports* | run the suggested commands on that machine, then repeat `owg doctor` |
| *It lists no graphics devices* | the driver is missing or too old for the build: update it, or run `scripts/install-llamacpp.sh --backend vulkan` |
| Other devices cannot open the console | open port 8440 (and 8442 for the API) in the console machine's firewall |
| *The console did not accept the code* | the code was used, mistyped or more than ten minutes old: make a new one |
| *A port openWeightGym needs (8440) is taken by another program* | stop the program that holds it (`sudo ss -ltnp "sport = :8440"` names it), often an older openWeightGym still running, then `owg restart PROGRAM` |
| Services stop when you log out | `sudo loginctl enable-linger $USER` |

## By hand: `install-suite.sh`

`install.sh` is a guide around [`scripts/install-suite.sh`](../scripts/install-suite.sh), which can
also be run by itself from a checkout. It discovers installed services, checks existing settings and
databases for compatibility before changing anything, keeps a copy of any unit or setting of
yours that it changes, and never switches a populated store to another database engine.

```bash
scripts/install-suite.sh                                   # asks, starting from what is installed
scripts/install-suite.sh --check --services compute        # audits and changes nothing
scripts/install-suite.sh --dry-run --services all          # shows what it would do
scripts/install-suite.sh --services front --database sqlite --topology front --yes --non-interactive
```

`--topology`, `--address`, `--front-url`, `--compute-url`, `--machine-name` and `--llama-server` write
new settings for a machine's part; without `--topology`, new settings are copied from
`settings/examples` with placeholders such as `compute.lan` for you to edit, and services whose
settings are new are not started until you have. `scripts/install-suite.sh --help` lists every
option, including PostgreSQL provisioning and the optional image and audio engines.

The machine key in `keys/machine.key` is what the console's gateway signs requests with and what
each GPU node checks them against; the console and each node must hold the same one (pairing
handles this). Keep `settings/*.toml`, `keys/` and `data/` private.

Moving existing deployments between machines, remote tool hosts, multi-machine telemetry, the
same-host second LoadCoach and identity portability are covered in the [operations log](operations.md).
