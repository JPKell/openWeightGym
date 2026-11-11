# Operations and release log

How this project's own installation is delivered and maintained, release by release: runtime
pins, migrations, qualification notes and recovery steps, newest notes first within each part.
To install openWeightGym on a machine of your own, see [Installation](installation.md).

## This deployment

This checkout starts a fresh Git history at `/home/jpk/ai/openWeightGym`. The original repository
and its history remain at `/home/jpk/ai/suite`; historical commit IDs and local qualification
receipts in these guides belong to that original installation.

Front is **k-srv, `10.77.10.42`**, installed under `~/openWeightGym`; the historical main compute checkout is
**`10.77.10.84`, `/home/jpk/ai/suite`**. Additional CPU workers appear in **Compute nodes**.

Current installation addresses, documented **2026-10-20**:

| Use | Address |
|---|---|
| Web console | [openWeightGym](https://10.77.10.42/) · [in-app help](https://10.77.10.42/docs/web-app) |
| Model API base | `http://10.77.10.42:8442/v1` |
| MCP tools | `http://10.77.10.42:8443/mcp` or `/mcp/TOOLSET` |
| Browser voice | [Private HTTPS voice app](https://k-srv.tailaf60c6.ts.net/) |
| Desktop voice | Front server, port `8447`; [download](https://10.77.10.42/static/LoadCoach-Voice.zip) |

### Offline page latency (2026-11-03)

Console reads now skip workers the passive node monitor has found unavailable. Accounts,
permissions and sessions are checked locally on every request; only model metadata is cached
under the current caller policy and refreshed in the background. Benchmark reads preserve the
no-wake header even with an injected HTTP client. Explicit compute requests retain live probing
and wake behavior. Recovery follows the configured monitor interval (10 seconds here); discovery
before the first completed poll, or without a configured monitoring account, still probes live.

Authenticated loopback HTTP on front, with compute unavailable: before is one request per route;
after is the mean of three. Eight HTTPS Chromium navigations from the workstation completed
loading in 51–159 ms with no JavaScript errors.

| Page | Before | After |
|---|---:|---:|
| Guide | 2.04 s | 44 ms |
| Appearance | 2.05 s | 28 ms |
| Status | 4.07 s | 60 ms |
| Chat | 4.09 s | 81 ms |
| Models | 8.13 s | 86 ms |
| Compute nodes | 8.14 s | 86 ms |
| Settings | 4.12 s | 56 ms |
| Benchmark runs | 8.20 s | 109 ms |

Validation: 193 targeted tests passed and one skipped; the file-store retry test also fails on
unchanged code and was excluded. Regression cases cover navigation without worker attempts,
deduplicated background refresh, policy changes, disabled accounts and node recovery. Live
invalid-key checks return 401; temporary audit sessions were revoked.

Delivered `benchmarks/app.py`, `turnstile/app.py`, `turnstile/machines.py` and `weightroom/app.py`
to front and restarted only Turnstile and WeightRoom after confirming an empty benchmark queue.
For rollback, reverse this patch in those four files and restart the same services, preserving
later unrelated edits.

Follow-up: Models, Usage, Requests and AI Logs now return an authenticated page shell before
fetching their contents. The Add a model form reads node engines and model-file
headers as a fragment of its own, after the page shell. Offline report refreshes also skip `POST /loadcoach/send-log` to
unavailable workers, removing another two-second timeout per report. Failed loads offer Retry;
each fragment rechecks the session and permissions. `complete=1` retains the full-data view.

After delivery, three authenticated loopback samples averaged 26–27 ms for these four shells;
their list/report fragments took 46–80 ms. HTTPS Chromium measured 52–131 ms to DOM readiness,
with working deferred charts and model-file controls. Opening the model editor still took
5.37 s for file inventory, without delaying initial navigation. Targeted server and browser
regressions passed; no live JavaScript errors or mobile overflow were found.

Civitai front-owned discovery now dispatches to Civitai instead of Hugging Face and applies
the caller's preview policy. Cards reject wrong-provider results rather than showing undefined
metadata. Live search returned 36 named versions with type/base-model fields. These changes
require matching WeightRoom templates/static files and Turnstile artifact routing; both front
services were restarted with an empty benchmark queue. Roll back their matching code and
assets together, preserving unrelated later changes; no migration or dependency is involved.

## Delivering services

Deliver `engines/`, its `llama_cpp/schema.json`, and the engine-specific
WeightRoom templates with matching LoadCoach/front code. Package and deployment copy lists
include them; no native engine build or new dependency is part of this extraction.
Existing `llama_server` configuration works unchanged. New per-node configuration may use:

```toml
[engines.llama_cpp]
enabled = true
executable = "/absolute/path/to/llama-server"
```

If `llama_server` is also present, its path must match. Console changes remain pending until
the selected worker's normal restart; they do not install software or interrupt active models.
Keep existing native binaries/libraries unchanged and qualify against the baseline.
No model database migration is added; retain the normal verified recovery set
and prior code/configuration for rollback without overwriting newer history.

Compute engine update (**2026-10-30**, owner-authorized after console polish): upstream
llama.cpp `344846b4fc6fff5988f215e7cf839f880d04decf` (`0.8.0-dev`) was pinned from master.
The primary CUDA build is under
`/home/jpk/ai/tools/llama.cpp-344846b4-20261030/build-cuda/bin`; it targets sm_120 and
keeps the existing CUDA include overlay. Both CPU hosts use the complete portable AVX2
bundle under `~/.local/share/suite/llamacpp/releases/344846b4fc6fff5988f215e7cf839f880d04decf-cpu-portable/bin`.
Its CUDA, native-CPU tuning and OpenMP backends are disabled; the remote worker has no
system OpenMP runtime. Version/help checks ran with `CUDA_VISIBLE_DEVICES` empty.
All three workers were restarted after draining their work and report the pinned runtime as
available; the benchmark queue was empty afterward. Per-node receipts are recorded in
`data/qualification/console-polish/llama-update.json`; no GPU inference qualification is claimed.

Each existing worker keeps its settings, model database, CPU/RAM policy and code release.
A private `loadcoach*.toml.before-llama-344846b4` preserves the previous configuration;
the only setting edit is `llama_server`. The matching user-service drop-in
`zzzzzzzzzzzz-llama-344846b4.conf` selects the new bundle's library directory, preventing
mixed old/new shared libraries. Updates drain existing work, restart the selected worker,
reopen it and revoke the temporary maintenance session. The prior binaries remain intact.
For rollback, wait for work/benchmarks to drain, restore only the previous `llama_server`
path from that private copy, remove this update's drop-in, run `systemctl --user daemon-reload`
and restart the corresponding LoadCoach service. Preserve any later unrelated settings edits.

Native final-answer grading correction (**2026-10-15**) updates the existing benchmark
worker, quality helper, regrader and imported-case grader; it adds no dependency or schema.
Finish active/queued benchmark plans before deploying the worker change because their
implementation hashes are pinned. Back up the benchmark store and deliver matching benchmark
code on front, then restart WeightRoom through the existing workflow. Startup automatically
re-scores ended quality runs with shared grader 3 (imported-case grader 2), preserving raw
outputs, native messages, prompts and timings without generation. Matching front worker and
grader code was delivered by 2026-10-16; the live benchmark queue was empty at review.

Named model load-outs (milestone 97, deployed **2026-10-16**) require matching LoadCoach API/CLI,
WeightRoom Dry fit templates/script and turnstile's explicit no-wake rule. Deploy through the
existing scripts; `loadcoach.models` migration 5 adds `model_loadouts` to compute's existing
core store. Include that store in the usual verified recovery set. Definitions and guarded undo
survive restart; set leases expire in memory and never automatically reload models after restart.
No new dependency, listener or credential is introduced. Before rolling back code, release active
set leases and review/restore applied settings if wanted. Older code ignores the definition table.
Bounded live protection, apply/undo, lease and shared-quant checks passed on 2026-10-16;
see [the model setup contract](loadcoach.md#validated-model-setup-and-presets-c05-milestone-93).

Estimated-wait admission (milestone 96, deployed **2026-10-16**) requires matching LoadCoach,
turnstile role routing, WeightRoom templates and defaults. No new store, dependency or migration
is needed. `max_estimated_wait = 0` preserves admission by default; collect load,
generation and native prompt-speed evidence on selected models before enabling a limit.
Unknown waits then receive 503 immediately, including cold models without a measured load.
Verify prediction headers, concurrent requests and probe preemption before relying on this
optional control. Disable/reset the setting to restore timeout-only admission; this does not
require reverting model settings or stored evidence. The live limit remains off; selected-model
calibration is required before enabling it.

C04 reusable case sets (milestone 94, deployed **2026-10-16**) is front-owned. On another
installation, deliver WeightRoom, the benchmark package, templates and static example/import
script together. Restart WeightRoom through the existing deployment workflow.
`weightroom.benchmarks` migration 4 automatically
adds `benchmark_datasets` in the existing benchmark store on SQLite/PostgreSQL. Include that
store in the normal verified recovery set. Existing compute task/evaluation APIs are reused;
no new dependency or listener is required. Frozen imported versions survive restart and newer
imports; source/worker changes can reject unfinished runs under the existing implementation
identity rule. Imported-case execution is verified on the retained A2 sample; its three cases
do not establish broader model separation. See the [case-import contract](loadcoach.md#reusable-benchmark-cases-and-regressions-c04-milestone-94).

C05 model setup (milestone 93, deployed **2026-10-16**) requires matching LoadCoach and WeightRoom
code/templates. Deploy and restart through the existing scripts; no new service, port or Python
dependency is needed. LoadCoach automatically runs `loadcoach.models` migration 4 in its existing
`loadcoach.core` store, adding `model_presets` and `model_setup_undo` on SQLite or PostgreSQL.
Take the usual verified recovery set before upgrading. Presets and undo records participate in
the existing full-store backup/restore and verified transfer workflows. Ordinary model edits
can invalidate an undo, which then refuses to overwrite the edit. Shared-preset migration 6
has the additional rollback requirements below.
Bounded live configuration, protection, apply/undo, lease and shared-quant qualification passed
on 2026-10-16; see [the model setup contract](loadcoach.md#validated-model-setup-and-presets-c05-milestone-93).
Neither preview nor applying a preset starts inference or certifies runtime compatibility.
Shared model presets (milestone 110, deployed **2026-10-16**) require matching compute code and
front templates. `loadcoach.models` migration 6 moves old presets into `shared_model_presets`
on the same core store, preserving collisions with source-entry suffixes; no new dependency or
service is needed. Include that store in the verified pre-upgrade recovery set. Same GGUF
name/architecture/parameter-size quants share settings, while the target keeps its model file,
projector and adapters. On another installation, check shared preview/apply/undo on chosen real quants.
Undo remains per entry. Rollback to code predating shared presets requires restoring the
pre-upgrade core-store recovery set, because the old preset table is emptied by migration.

C03 request diagnostics (milestone 92) was confirmed complete by the owner **2026-10-15**
and removed from pending Progression. For new or older installations, it requires matching
code on compute and front: LoadCoach records timing and model metadata; turnstile presents
the retained evidence; WeightRoom renders the existing request page. Deploy and restart these three programs
with the existing deployment scripts. This change adds no listener, dependency, store or schema
migration. It builds on milestone 91's front event-log ownership; installations predating that
move must follow its existing log migration procedure. Older requests remain readable but
cannot acquire detailed timing retroactively. Verify a new cold and warm request, a stream,
a failure, authorization and retained diagnostics with compute unavailable after delivery.
Service-output links use the existing Spotter journal API and permissions. The completion
status records owner confirmation; the implementation task's local tests are separate evidence.

Run the deployment installer from an openWeightGym checkout:

```bash
scripts/install-suite.sh
```

The installer scans this account's systemd user units, including the legacy `frontdesk` and
`tooldoor` aliases. Installed openWeightGym services are preselected. On a new machine it offers compute,
front, or all services as the starting set, then shows every service so the selection can be
overridden.

**A fresh machine from a git clone.** [`install.sh`](../install.sh) is the usual way now (see
[Installation](installation.md)); it runs `install-suite.sh` for you. To run it by hand, clone the
repository and run the installer from its root; the clone is the installation. Checked 2026-10-22 in a clean `python:3.13-slim` container for
both sets:

```bash
git clone https://github.com/JPKell/openWeightGym.git ~/openWeightGym && cd ~/openWeightGym
scripts/install-suite.sh --services front --database sqlite --non-interactive --yes     # front
scripts/install-suite.sh --services compute --database sqlite --non-interactive --yes   # a compute machine
```

It makes `.venv`, renders settings from the examples, makes the SQLite stores and installs and
enables the units; newly configured services are not started until their settings are reviewed.
It also makes, only when absent, `keys/machine.key` (front and each compute machine must hold
the same one: copy front's to the compute machine, owner-readable only) and, on front, the
`weightroom` account with `keys/weightroom.key`. Then add yourself
(`.venv/bin/weightroom user add NAME superuser` and `.venv/bin/weightroom user password NAME`)
or bring existing accounts with `scripts/import-accounts.sh`, and start the services. A fresh
LoadCoach starts with no models; the defaults file shows example model sections only.

### Optional image generation runtime

The initial installer asks compute/all installations whether to install **stable-diffusion.cpp**.
Fresh installs default to keeping image support absent. Unattended installation uses
`--sdcpp install|skip|keep`; omission and `--yes` alone preserve an existing runtime without
downloading one. `--sdcpp-backend cuda|vulkan|cpu` explicitly chooses the backend; CUDA is the
default and missing CUDA requirements never select CPU implicitly. The native runtime belongs
on compute; rendered user assets belong on front.

Install later from the openWeightGym root:

```bash
scripts/install-sdcpp.sh --backend cuda
scripts/install-sdcpp.sh --check
scripts/install-sdcpp.sh --dry-run --backend cuda
```

Linux prerequisites are Git, CMake, a C++ compiler and a compatible NVIDIA driver/CUDA toolkit
providing `nvcc`. Distribution build tools can be installed with
`sudo apt install git cmake build-essential`; install the toolkit matching the local driver
separately. Vulkan needs its development headers/toolchain. CPU is an explicit backend choice.

The script pins commit `95ff76230889898e1bf40058ba272a3699b50dc6`, verifies executables,
preserves immutable releases under `runtimes/sdcpp/releases` in the installation (`--prefix` chooses
another folder), and changes only
`[engines.sdcpp]` in `settings/loadcoach.toml`, retaining a private configuration backup. It
neither restarts services nor downloads weights. An optional-build failure is reported
separately from completed base installation work; fix requirements and rerun the script. `--check`
verifies the release that `[engines.sdcpp]` names, not whatever an older installation left elsewhere,
and reports a broken one in a single line rather than stopping the installer's check.

openWeightGym's default build includes a content-verified layer-placement patch. Explicit positive
`layer_weights` ratios, such as `[3,1]`, permit unequal diffusion placement independent of live
free VRAM. Indivisible layers may prevent an exact ratio. Upstream-only manual builds remain
supported, but do not provide openWeightGym's fixed-ratio sharing contract. Effective splitting is checked
from native execution; a silent one-card fallback fails the requested two-card workflow.

Unqualified workflows reserve assigned cards. Sharing requires fixed placement and a measured
maximum workflow envelope, including the permitted reference count/target, generation dimensions,
configured adapters and maximum upscale. Reservations include measured peaks plus 256 MiB
and the node's existing card margin; host RAM remains capped for the full owned process session.
Changes to components, bounds, runtime binaries or orchestration invalidate qualification.

Image model files use `<models_folder>/image` and, when configured,
`<models_storage_folder>/image`. Source/revision sidecars belong beside downloaded components.
For private Civitai downloads, put the administrator token in an owner-readable file on the
artifact-owning LoadCoach and configure `civitai_token_file` in that node's LoadCoach settings;
relative paths resolve from the settings directory. The token is sent only as provider
authorization, never to browser fields or download URLs. An absent token still permits public access.

For a manual build:

```bash
git clone --recursive https://github.com/leejet/stable-diffusion.cpp.git ~/stable-diffusion.cpp
git -C ~/stable-diffusion.cpp checkout --detach 95ff76230889898e1bf40058ba272a3699b50dc6
git -C ~/stable-diffusion.cpp submodule update --init --recursive
cmake -S ~/stable-diffusion.cpp -B ~/stable-diffusion.cpp/build-suite \
  -DCMAKE_BUILD_TYPE=Release -DSD_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=native \
  -DSD_BUILD_SHARED_LIBS=OFF -DBUILD_SHARED_LIBS=OFF \
  -DSD_SERVER_BUILD_FRONTEND=OFF -DSD_WEBP=OFF -DSD_WEBM=OFF
cmake --build ~/stable-diffusion.cpp/build-suite --target sd-cli sd-server --parallel 4
~/stable-diffusion.cpp/build-suite/bin/sd-server --help
scripts/install-sdcpp.sh --source ~/stable-diffusion.cpp \
  --adopt-build ~/stable-diffusion.cpp/build-suite --backend cuda --upstream-only
```

For Vulkan replace CUDA options with `-DSD_VULKAN=ON` and register with `--backend vulkan`;
for CPU omit GPU build options and register with `--backend cpu`. To manage binaries manually,
set `[engines.sdcpp]` with `enabled = true` and absolute `executable`/`cli` paths to `sd-server`
and `sd-cli`. Binaries must be executable by the service account, with matching runtime
libraries. LoadCoach launches private listeners and supervises the entire process session.

Verify installation with `--check`, then use a configured small model for an opt-in smoke
generation after queued work drains. Acquire diffusion weights, text encoder, VAE, LoRAs and
upscalers separately through Files; all components/formats must validate before use. Effective
model splitting requires separate GPU qualification; accepting a split flag is insufficient.

Updates use a newly qualified pinned revision and a new release directory. To roll back,
restore the private configuration preimage or select the previous release's executable paths;
restart/reload an affected idle LoadCoach after active work drains. Retain model/user assets.
An unavailable binary/toolkit is reported as unavailable optional image support.

### Optional audio runtime (SoundRoom)

The initial installer asks compute/all installations whether to install **audio.cpp** for
music, sounds, speech and transcription ([plan](soundroom-plan.md)). Fresh installs default to
keeping it absent. Unattended installation uses `--audiocpp install|skip|keep`; omission and
`--yes` alone preserve an existing runtime without downloading one. `--audiocpp-backend
cuda|vulkan|cpu` chooses the backend explicitly; CUDA is the default and a missing toolkit never
selects CPU implicitly. Upstream publishes no Linux CUDA binary, so the script builds from source.

```bash
scripts/install-audiocpp.sh --backend cuda --cuda-architectures native
scripts/install-audiocpp.sh --check
scripts/install-audiocpp.sh --dry-run --backend cuda
```

Prerequisites are those of the image runtime above. The script pins audio.cpp v0.9.2 (commit
`0097d365a00b52a6991ad7618f0a04e8d2935e47`), builds `audiocpp_server` and `audiocpp_cli` with
every model family (`AUDIOCPP_MODEL_SET=full`) and without the optional server frontends or
upstream model manager, using 6 build jobs by default (`--jobs`; never an unbounded `-j`). It
copies the pinned `model_specs/` catalogue into the release, verifies executables, preserves
immutable releases under `runtimes/audiocpp/releases` in the installation (`--prefix` chooses another
folder), and changes only
`[engines.audiocpp]` in `settings/loadcoach.toml`, retaining a private configuration backup.
It neither restarts services nor downloads weights. The one upstream submodule (server
frontends, an SSH URL) is not fetched.

For a manual build:

```bash
git clone https://github.com/0xShug0/audio.cpp.git ~/audio.cpp
git -C ~/audio.cpp checkout --detach 0097d365a00b52a6991ad7618f0a04e8d2935e47
cmake -S ~/audio.cpp -B ~/audio.cpp/build-suite -DCMAKE_BUILD_TYPE=Release \
  -DENGINE_ENABLE_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=native -DAUDIOCPP_MODEL_SET=full \
  -DAUDIOCPP_BUILD_SERVER_FRONTENDS=OFF -DAUDIOCPP_BUILD_NATIVE_MODEL_MANAGER=OFF
cmake --build ~/audio.cpp/build-suite --target audiocpp_server audiocpp_cli --parallel 6
~/audio.cpp/build-suite/bin/audiocpp_server --version
scripts/install-audiocpp.sh --source ~/audio.cpp --adopt-build ~/audio.cpp/build-suite --backend cuda
```

For Vulkan replace the CUDA options with `-DENGINE_ENABLE_VULKAN=ON`; for CPU omit GPU options.
`audiocpp_server` has no authentication: LoadCoach binds it to loopback on a private port and
never exposes it. Model packages are downloaded separately through Files. audio.cpp runs one
model on one device; component placement across cards needs separate qualification. Updates use
a new pinned revision and release directory; roll back by restoring the configuration preimage
or the previous release's paths, then restart an idle LoadCoach after work drains.

Audio packages install from **Models → Audio models**, or on compute from the command line with
the same verified installer (`python -m soundroom.packages list|install|remove FAMILY/PACKAGE
--place MODELS_FOLDER/audio`). Each file is checked against Hugging Face's SHA-256 (or its git
hash for small sidecars) before the package moves into place; a file already present with the
same hash is adopted, not downloaded again. A profile is then made on the same page: one installed
package, its kind of work and its card or the CPU.

**Accounts for a test installation.** `scripts/export-accounts.sh OUT.tar` on front copies the
groups, users (password hashes, authenticator secrets), API-key hashes, spending allowances and
the `keys/` files; `scripts/import-accounts.sh OUT.tar ROOT` loads them into another front
installation, replacing same-named rows and copying only key files it lacks. No other secret
or seed is involved: passwords are salted scrypt hashes and API keys plain SHA-256 hashes, so
they check anywhere. The browser cookie secret (`data/secret.json`) is not carried; people sign
in again. The tar is not encrypted: keep it private. `suite-identity` (below) is the encrypted,
complete move of a host to the same paths.

The obsolete `frontdesk/` Python import shim is no longer packaged or copied to front.
Both deployment modes run [the guarded cleanup](../scripts/prune-frontdesk.py) after
installing canonical package metadata. Cleanup removes only the known shim and its bytecode;
it refuses symlinks, unexpected files or a missing `weightroom/app.py`. It never deletes
settings, keys or databases. Python callers use `weightroom`; the old command/service names
and saved-settings/browser-preference migration readers remain compatible without the folder.
Front's old home-directory symlink was retired separately on 2026-10-14 after canonicalizing
and privately backing up its live configuration paths and reloading the affected idle services.
That cleanup reloaded turnstile and voice-gateway without restarting WeightRoom. Sign-in, turnstile
model listing and browser voice configuration returned HTTP 200; all three front services
remained active and existing key contents were unchanged. Private configuration recovery copies
are retained on front under `~/.local/share/suite-cleanup/`. Seventeen focused cleanup,
rebrand, installer and documentation checks passed, followed by eight final guard/documentation
checks; a built wheel contained WeightRoom and no obsolete source package.

On **2026-10-14**, the owner removed the local project's historical backup and generated
artifact inventory (milestone 85), including tracked `build/lib` and `dist` output. The old
wheel still contained the retired `frontdesk` and `relay` packages; new wheels are built
from canonical source. `build/`, `dist/` and package metadata are ignored and should not be
committed. The cleanup also removed local migration rollback copies and three recovery
sets; create a new local set before relying on backup freshness. Front's installation and
private recovery directories were outside this cleanup's scope.
Verification passed the full **701-test suite with zero skips**, including disposable
PostgreSQL 16 native dump/restore and headless browser/Tk tests; seven installed service
commands also answered `--help`. PostgreSQL tools were supplied temporarily through the
disposable container, not installed on either host. Test fixtures were corrected to reopen
accounts through the shared database facade and isolate the second console's connection;
the stale frontend script-version assertion now matches v5. No application code changed.
One existing Starlette/httpx deprecation warning remains. A fresh wheel built in an isolated
temporary source tree matched 283 current package files, omitted retired import packages,
and retained supported legacy command aliases.

The first front deployment after retiring that symlink exposed an incomplete virtual-environment
path cleanup: `pip` still began with `#!/home/jpk/frontdesk/.venv/bin/python3`, so the install
stopped with `bad interpreter`. On **2026-10-14**, 19 generated launchers, activation files and
`pyvenv.cfg` were corrected to `/home/jpk/weightroom/.venv`, with owner-only recovery copies
under `~/.local/share/suite-cleanup/venv-paths-*`. Both direct pip and Python's pip module were
verified on front, without reinstalling packages or restarting services. This repair did not
complete the interrupted code deployment. Rerun `deploy-front` to install and activate it.
Both front deployment modes and `install-suite.sh` now invoke `.venv/bin/python -m pip`
instead of relying on pip's generated interpreter path. They upgrade pip to at least 26.2.1
before installing openWeightGym; benchmark installations pin NLTK 3.10.3. See the
[security sweep](application-sweep-2026-10-20.md) for qualification and the remaining upstream advisory.
Fourteen focused installer, cleanup
and documentation checks passed. A moved virtual environment can retain absolute paths in
all its generated launchers and activation scripts; removing a path alias requires inspecting
those as well as service and configuration paths.

Host-fingerprinted telemetry (milestone 123) upgrades front's existing event-log store to
schema version 5, adding telemetry and host-registry tables without rewriting the audit chain.
See [host setup](#multi-machine-telemetry) and the [telemetry contract](loadcoach.md#physical-host-telemetry).

### Model-file storage

Front model storage is optional. Under **Settings → General → Model file locations**, choose
**Front machine** and set an absolute cache path, storage path, or both. Empty disables a tier
without deleting its files. Front creates the standard model subfolders and uses them immediately;
no inference worker is required. The equivalent settings are `turnstile.models_folder` and
`turnstile.models_storage_folder` in `settings/weightroom.toml`.

`turnstile.artifact_machine` can explicitly select `front` or an existing file-owning worker.
Without an explicit selection, configured front storage is preferred, then `front_cpu`, then
`compute`. Files remains available when a store is disabled or offline, with setup links and
per-node availability. Distribution still requires an answering destination Spotter.

Choose a compute node in the same location editor to save its paths while offline. Front retains
the change and delivers it without waking that node when it reconnects, rechecking the saving
account's permission. The node applies the paths after its compute service restarts. Changing
folders does not move model weights or rewrite configured model paths; retain old copies until
models have been updated and verified. Paths must be absolute. The cache and storage may be the
same folder, which then holds every file once and counts as both; two different folders must not
hold one another. Storage keeps every model file: place copies in the cache and on nodes in bulk
from the Storage view on Files.

Multi-node Status requires matching Turnstile/WeightRoom on front and existing LoadCoach status/
Spotter readings; no new service, store, key or port is required. For upgrades or added nodes,
verify GPU-loaded, CPU-loaded and unavailable nodes, secondary-node lock/unlock, and selected-node
drain/clear/unload-all/resume without affecting primary. See the [runtime contract](loadcoach.md#physical-host-telemetry).

Milestone 82 (local code **2026-10-14**) changes LoadCoach's default `model_idle_minutes` to
**0**: models stay resident until memory is needed, then idle models can be replaced
immediately. Existing explicit positive file or saved values are retained as optional delays.
The owner requested zero on the current compute service; it was applied through the live
settings API without restarting, with the previous value backed up privately. Updated role
routing additionally supports per-role `loaded_bonus` (default 25%) and `fast_loaded_bonus`
(default 100%, ×2). No database migration or new credential is required. Matching LoadCoach, Turnstile and WeightRoom Roles-form code is deployed on this installation.
On an older installation, update those components together before editing these fields; an
old compute service does not accept the new role properties.

Before changing the machine it checks:

- Python 3.13 or newer and any existing virtual environment;
- existing unit paths and masks;
- TOML syntax for the selected services;
- SQLite readability, `quick_check`, and known migration versions;
- PostgreSQL connectivity, DSN-file permissions, known migration versions, and compatible
  `pg_dump`/`pg_restore` tools when running `--check` or the final post-install audit.

An unknown future schema version is a hard error. Missing configuration on a new installation is
a warning; the example is copied, and its service is installed but not started until the new file
has been reviewed. Existing settings, data, and keys are never overwritten by example files.
Settings and service units that the installer changes receive timestamped backups.

## Request filters and client access (milestone 150)

Deploy matching WeightRoom, Turnstile, `loadcoach/` and `db/` code through the existing
workflow after benchmark/request work drains. `loadcoach.users` migration 6 adds the
portable `client_accesses` table and indexes to the existing front accounts store; no new
store, listener or secret is introduced. Keep Turnstile's `auth_key_file` pointing to the
same private API key WeightRoom loads through `key_file`, and update existing release-source
overrides before restarting affected services. Previous code rejects the newer account schema;
complete forward delivery rather than mixing old/new account writers. The owner's disposable
installation [waives deployment backups](application-sweep-2026-10-20.md#deployment-requirements).
See the [request contract](loadcoach.md#request-filters-and-client-access-milestone-150) for
provenance, privacy and filter behavior.

## Observability and traceability (milestone 151)

Deploy matching shared `loadcoach`, `db`, front, voice and plugin code plus WeightRoom templates
on all configured nodes. Existing log owner stores apply portable LOG migration 7; SQLite and
PostgreSQL qualification are separate from live engine selection. Do not run older writers
against the upgraded schema. Recovery requires a schema-compatible release; preserve private
settings, pairing keys and inactive services. No new daemon, public listener, dependency,
credential or database authority is introduced.

Update installed standard-library tool hosts too: their HTTP responses now carry ephemeral
ingress IDs. Spotter also returns IDs while retaining its existing telemetry storage policy;
ToolLocker owns account-attributed tool timelines.

Rebuild/update installed adapter bundles as well as checkout source: their minimal `loadcoach`
package is now required. Preserve per-instance settings and private keys. The existing controller
collects optional adapter spans; an older adapter without that endpoint remains usable with
incomplete telemetry. Existing signed node forwarding transports compute observations.

Voice processes may opt into their existing owner's store with root-level
`observability_database` (SQLite path) or `observability_dsn_file` (existing PostgreSQL DSN file),
and `observability_machine`. Configure the compute voice service against its compute log store
and the gateway against the front accounts/log store. Without an owner store, response IDs
remain available but detailed voice spans are not persisted. Gateway `context_key_file` points
to the existing private front proof key to authenticate continuity to Turnstile; never send it
to a browser. Example files document these optional settings.

An operator may set service environment `SUITE_OBSERVATION_RECORDING=off` to disable optional
span recording for controlled overhead comparisons or recovery; response IDs and required audit
writes remain enabled. Remove the override and restart to restore recording. A disabled source
provides no new detailed evidence and must not be interpreted as healthy observation coverage.

Observe source health/backlog and optional-loss counters after restarts. Preserve existing
benchmark pause state and drain admitted work before activation. The current disposable-install
backup waiver applies; the later owner clean reinstall remains a separate check. See the
[operating contract](loadcoach.md#observability-and-traceability-milestone-151).

## Encoder library and relocation recovery

Cache and storage roots include `encoders` and `storage_encoders`. Dedicated embedding weights
and detached image text encoders retain complete package directories, tokenizers and configs;
whole diffusion checkpoints and VAEs stay under Image. GGUF tensor metadata and configured
consumers preserve image-component roles after relocation. Shared disk files still have separate
process/GPU reservations. A full text model may support another embedding use only after native
vector and bounded similarity qualification; stripped image encoders are not advertised as embedders.

The installed `mistral-image-encoder-embedding` entry reuses the full Mistral image encoder
through the existing llama.cpp engine, with mean pooling, normalized vectors and a 512-token
input cap. [Native shape/repeatability/similarity checks](../data/qualification/model-image-settings-20261027/encoders.json)
passed; this general text model is not an embedding-tuned retrieval-quality claim. The stripped
Klein component lacks the standalone architecture/tokenizer contract and remains image-only.

`loadcoach/encoder_migration.py` plans across every answering node, detects moving files,
conflicts and insufficient room, copies/verifies hashes and rewrites current references through
`Models.put`. Stable IDs and historical evidence remain. A selected-file relocation advances its
current revision; prior path-keyed measurements are historical unless an explicit equivalence
receipt verifies all files, unchanged runtime/settings and native consumers. The release's
machine-readable manifests retain old/new paths, hashes, original settings and verification.

For recovery, drain affected work and stop its LoadCoach service. Load the node manifest and
call `restore_files(node, record)` before `rewrite(node, Models(database), record, rollback=True)`.
The helper refuses different existing consumers or files. Restore the compatible release pin,
reload units and restart affected services; verify signed status, embeddings and image generation.
Keep current databases/history and the verified encoder library. Never restore an old database
over newer work. Superseded files are removed only after every affected consumer is verified.

## Runtime source overrides

Existing services can select a reviewed release through a scoped `PYTHONPATH` unit drop-in.
Inspect and back up the effective unit/drop-ins before deployment: copying checkout files alone
does not replace the selected release. Deliberately update that source override, drain active
work and restart only the affected services through the existing deployment workflow.

For rollback, restore the saved override to a compatible prior release without overwriting newer
databases or history. Preserve unrelated drop-ins, settings, native engine files and resource
limits; do not remove delegated memory groups until all owned children have exited. Protected
recovery sets remain under `~/.local/share/ai-suite/recovery/` on the affected hosts.

### Usability release 2026-10-30

The V1–V16 console release selects commit `5d29419919fb55b7ae7f4e6989c2a91c1340a77f`
at `~/.local/share/ai-suite/releases/usability-5d294199/source` through WeightRoom's
`zzzzzzzzzzzzzzzzz-usability-5d294199.conf`. All 755 files, imports and existing dependencies
were verified. Only WeightRoom was restarted; Voice gateway, workers, native engines,
private settings and databases retain their existing releases/contracts. No migration is added.

Activation drained front chats and checked an empty benchmark queue; the previous benchmark
pause state was restored. Private unit preimages/activation records are retained under
`~/.local/share/ai-suite/recovery/usability-5d294199/`; the earlier usability cutovers retain
matching `usability-cd4b3969/` and `usability-63b2f597/` recovery sets. Qualification/status
is recorded in [the usability execution record](../makeBetter.md#execution).

For rollback, drain affected work, preserve the benchmark pause state and remove only the
new `zzzzzzzzzzzzzzzzz-usability-5d294199.conf` override to return to the previous usability
release. To return to the earlier UI consistency release, also remove
`zzzzzzzzzzzzzzzz-usability-cd4b3969.conf` and `zzzzzzzzzzzzzzz-usability-20261030.conf`.
Reload user units and restart only WeightRoom; verify sign-in, a permitted page and its assets,
then restore the prior pause state. Preserve unrelated drop-ins and current data; no database
restore is needed for this UI release.

### UI consistency release 2026-10-30

The shared-component UI release pins commit `7b968d01353bba1e358653a966854808c2d0e468` at
`~/.local/share/ai-suite/releases/ui-consistency-7b968d01/source` on front. WeightRoom and
Voice gateway select it through `zzzzzzzzzzzzzz-ui-consistency-20261030.conf`; worker services
retain their prior releases and native runtimes. All 754 release files were hash-verified;
imports and installed dependencies passed. No dependency, database or settings migration is added.

Activation checked an empty benchmark queue, no active front chat and no active/starting voice
calls. The benchmark worker's previous pause state was restored. Unit preimages and activation
records are private under `~/.local/share/ai-suite/recovery/ui-consistency-7b968d01/` on front.
Local regression evidence remains in [the execution record](../makeBetter.md#execution);
live verification is recorded in `data/qualification/ui-consistency/deployment.json`.
All 57 authenticated pages and 342 responsive/theme cases passed at 1440/390 in Slate light/dark
and Forest light, without layout failures, JavaScript errors or HTTP 5xx responses. Served console
assets and the standalone Voice index/script/CSS matched the reviewed release. Both services
remained active, the benchmark queue remained empty and the temporary verification session was revoked.
These browser checks do not establish subjective owner acceptance.

For rollback, wait for front chat, calls and benchmarks to drain; remove only this release's
named drop-in from `weightroom.service.d` and `voice-gateway.service.d`, run
`systemctl --user daemon-reload` and restart those two units. Preserve the earlier drop-ins,
resource limits, settings, databases, credentials and native-runtime selections.

### Console polish release 2026-10-30

The console polish release pins commit `25ebb0c36f906ba8d136cc938f2894e483aea1e8` at
`~/.local/share/ai-suite/releases/console-polish-25ebb0c/source` on compute, front and remote CPU.
Only four affected services select it through
`zzzzzzzzzzzzz-console-polish-20261030.conf`: front `weightroom.service`,
`loadcoach-front-cpu.service`, primary `loadcoach.service` and remote
`loadcoach-remote-cpu.service`. The worker update supplies the audio-family option metadata
used by the new editor. Unchanged services retain their preceding reviewed source.
All 754 release files matched on each host; imports and installed dependency checks passed.

Activation drained worker work, checked an empty benchmark queue and paused the benchmark worker
briefly, restoring its original pause state afterward. Service CPU/RAM limits, private settings,
credentials, databases and the selected llama.cpp builds/library paths were preserved.
[Deployment and live checks](../data/qualification/console-polish/deployment.json) retain
service states, source parity and browser results: 31 authenticated pages and 180 responsive/theme
cases passed without JavaScript errors, HTTP 5xx responses or page overflow. The temporary
session was revoked, all workers remained ready and the benchmark queue was empty afterward.
Browser checks use an expiring session,
read-only page navigation and CPU-rendered Chromium; they do not establish subjective owner acceptance.

For rollback, first drain work and benchmarks, remove only this release's named drop-in from
those four units, run `systemctl --user daemon-reload` and restart the same units. Earlier source
and native-runtime drop-ins remain in place. Unit preimages and activation records are private
under `~/.local/share/ai-suite/recovery/console-polish-25ebb0c/` on each affected host.
Do not restore older databases or overwrite newer settings as part of a code rollback.

### Module audit release 2026-10-30

Compute, front and remote CPU select runtime commit `a1ae47646bb5d073e07cb8ad2cca12ef2f6dedec`
from `~/.local/share/ai-suite/releases/module-audit-a1ae476/source` through
`zzzzzzzzzz-module-audit-20261030.conf`. Twelve active services were restarted after work drained;
inactive services were pinned for their next start. The separate front Hermes adapter received
its five changed runtime/contract files. Native binaries, settings, credentials, databases and
resource limits were preserved. [Live verification](../data/qualification/module-audit-20261029/deployment.json)
and the [module audit](application-sweep-2026-10-20.md#module-audit-and-reasoning-discovery-started-2026-10-29)
record coverage and limits.

For rollback, drain active work, remove only this release's named drop-in, reload systemd and
restart the previously active services. Preserve earlier drop-ins. Restore the separate adapter's
original files using the existence map/preimages under
`~/.local/share/ai-suite/recovery/module-audit-9bbfdcc/`; remove newly introduced files only where
that map says they did not exist. Keep live databases/history: node discovery is an additive
migration. Original unit preimages are in that same recovery directory; final activation records
are under `recovery/module-audit-a1ae476/`. The incomplete `module-audit-9bbfdcc/source` archive
is **not** a rollback target. Do not rerun an installer migration against a mismatched schema.

Model/image deployment **2026-10-27**: seven core/telemetry services across compute, front and remote CPU
initially selected immutable runtime `model-image-cdbd689f/source`; front WeightRoom selected
`model-image-374de66f/source`. The respective overrides are `zzzzzz-model-image-20261027.conf`
and `zzzzzzz-model-image-ui-20261027.conf`. [Verification and recovery receipts](../data/qualification/model-image-settings-20261027/report.json)
record exact commits, source hashes, dependencies, tests, native qualification and live checks.
Native runtimes, private configuration, resource limits and current user inference were preserved.

Recovery preimages and node relocation manifests are under
`~/.local/share/ai-suite/recovery/model-image-20261027/` on each affected host. For a front-only
rollback, restore the saved `ui-weightroom.service.d` override; the original core-service
drop-ins are retained separately. A rollback across the encoder move also requires the file/reference
restoration above before starting older code. Preserve current databases and verified library copies.
The [preceding model-management release](../data/qualification/models-ui-20261026/report.json)
retains its own earlier qualification limits.

Passive-alert correction **2026-10-27**, runtime `0a26d662`: Turnstile and WeightRoom selected
`~/.local/share/ai-suite/releases/passive-alerts-0a26d662/source` through
`zzzzzzzz-passive-alerts-20261027.conf`; the six LoadCoach/Spotter services retain their model/image
release. Alert collection previously omitted the no-wake header and sent Wake-on-LAN during
each observed idle-sleep cycle. Collector headers and Turnstile's passive-route guard prevent
those alert wake-ups. All 63 focused checks passed (one skipped), dependencies and live monitoring HTTP passed.
The workstation's 60-minute idle policy is unchanged; no hardware sleep/wake test was performed.
Recovery records and previous front overrides are under
`~/.local/share/ai-suite/recovery/passive-alerts-0a26d662/`. After affected work drains, removing
only this correction's override and restarting the two front services restores their retained prior
pins. Preserve current data/settings and the encoder relocation recovery contract above.

Plugin-refresh correction **2026-10-28**, runtime `b5cfa311`: the first correction was active,
but the next natural cycle logged a wake from `/loadcoach/plugins/mirror`. The shared snapshot
refresh now sends no-wake and Turnstile independently protects that route. Turnstile and WeightRoom
select `~/.local/share/ai-suite/releases/passive-refresh-b5cfa311/source` through
`zzzzzzzzz-passive-refresh-20261028.conf`. All 105 focused checks passed, including both background
watchers with compute becoming unavailable between reads, cached-snapshot preservation and
ordinary inference's retained wake behavior. Dependencies and live monitoring/mirror HTTP passed;
the idle policy and other service processes remain unchanged. No physical sleep/wake test was run.
Recovery preimages and verification are under `~/.local/share/ai-suite/recovery/passive-refresh-b5cfa311/`;
after work drains, removing only this correction's override and restarting those two front services
restores the previous alert-only correction. Keep current data and settings.

For recovery of the preceding **2026-10-26 model-management boundary**, first wait for benchmark
work, active requests and holds to clear. Compute's
previous committed source is `data/qualification/models-ui-20261026/compute-before.tar`;
extract it into a private recovery directory. Front's prior installed code is in
`~/.local/share/ai-suite/recovery/models-ui-20261026/code`. Point the affected compute/front
release pins at those directories rather than falling back to the newly copied installation.
On the remote CPU, remove only `zzzz-models-ui-20261026.conf` to restore its retained earlier
source pins. Reload units, restart only the affected services and verify readiness. Keep
current databases, history, model files, keys and settings; an older release may not support
the newly added Kokoro profile. Dependency and effective-unit records are retained alongside
the recovery copies.

API reference deployment **2026-10-26**, runtime commit `b3efeeb2`: WeightRoom on front
selected `~/.local/share/ai-suite/releases/api-docs-b3efeeb2/source` through
`zzzzz-api-docs-20261026.conf`. Only WeightRoom restarted; Turnstile, voice gateway and
front CPU worker processes retained their PIDs. Dependencies did not change. All 23 focused
checks passed; authenticated live HTTP/Chromium checks verified `/docs/api`, 222 custom
operations, source navigation, 320/390-pixel layouts, private access and the desktop download.
[Verification](../data/qualification/api-docs-20261026/report.json) and private front recovery
records under `~/.local/share/ai-suite/recovery/api-docs-20261026/` retain the effective unit,
dependency list and before/after service state. To roll back, remove only the new
`zzzzz-api-docs-20261026.conf`, reload units and restart WeightRoom after affected work drains;
the retained `zzzz-models-ui-20261026.conf` restores its prior source. Preserve current data,
keys, settings and the earlier release.

## Multi-machine telemetry

Install matching LoadCoach/Spotter code on each monitored host and Turnstile/WeightRoom/`db/`
code on front. Give each host private Spotter settings referencing one local LoadCoach settings
file and its machine key, a host-specific listening address/port, and only permitted `[services]`.
Primary compute uses port 8444; the existing front/VPS Spotters use 9444. Keep `sleep_after = 0`
unless that host intentionally sleeps; adding monitoring must not change its power policy.

Add the private `spotter` URL to the corresponding `[machines.NAME]` in front's
`settings/weightroom.toml`; requests use that node's existing machine key. Several LoadCoach
instances on one host retain separate node identities but share one physical-host fingerprint
and telemetry card. Start/check Spotter before restarting Turnstile, after requests and queued
benchmarks drain. Verify signed responses and matching LoadCoach/Spotter fingerprints, current
bars, host-registry/history growth and retained offline history. A live Spotter must not make
an unavailable LoadCoach eligible for placement. Identity/storage/UI details are in the
[telemetry contract](loadcoach.md#physical-host-telemetry); completed historical imports are
recorded under [milestone 128](database-storage.md#front-toollocker-and-remaining-telemetry-milestone-128).

## Same-host two-LoadCoach rig

This optional rig tests placement using two instances on one compute host, one GPU each; it
does not qualify another physical GPU host. Use separate settings, data/database, token, port
and machine-key files, with stable `machine_name = "compute"` and `machine_name = "rig"`.
Names identify signed events and public request numbers. Keep the primary on `CUDA0` and rig
on `CUDA1`; never leave two schedulers claiming the same card.

Before changing assignments or starting/restarting any affected service, drain benchmark and
request work and save the original settings and effective unit/drop-ins. Validate one-card
model settings first: entries requiring two cards can prevent primary startup. If needed,
use a temporary one-card configuration and scoped `ExecStart` override that still opens the
existing primary model database; do not replace that database with an empty rig library.
Roll back a failed startup immediately. Preserve any [release-source overrides](#runtime-source-overrides).

The ignored `settings/loadcoach-rig.toml` may use:

```toml
host = "0.0.0.0"
port = 9442
data = "../data/rig"
machine_name = "rig"
machine_key_file = "../keys/rig.key"
front = "http://10.77.10.42:8442"
gpus = ["CUDA1"]
```

The optional [loadcoach-rig.service](../services/loadcoach-rig.service) uses this checkout.
Install it under `~/.config/systemd/user/`, reload the user manager and start manually; do not
enable it at login. A new rig data directory has an empty model library: register the existing
weights through `loadcoach --config FILE models add`, including `ctx-size`. Confirm both
inventories report the same verified weights SHA-256 before treating copies as interchangeable;
an unhashed resident model or an equal name alone is insufficient.

On front, copy the rig key into its matching owner-only file and add:

```toml
[machines.rig]
compute = "http://10.77.10.84:9442"
machine_key_file = "../keys/rig.key"
```

Primary and rig keys must differ. Front's account key must permit status/models/facts on both.
Deploy matching code to front and both workers, start the rig and restart Turnstile in the
drained window. Verify both nodes become ready; route requests with each copy resident and
check public request IDs, Requests/capture, Status, Peek, Diagnostics and Stop. Check fallback
with the preferred node unavailable before output, then restore it. Stream failure after
output must end without replay; no destructive or physical power test is authorized here.
Placement and per-location benchmark rules are in the [code guide](loadcoach.md#machine-inventory-milestone-112);
benchmark tasks retain their [frozen-node contract](loadcoach.md#external-benchmark-task-contract).

After checks, stop the rig, remove `[machines.rig]`, restart drained Turnstile, restore primary's
original GPU settings and saved unit/drop-ins, remove only the temporary one-card primary
configuration/`ExecStart` override, and restart primary. Verify both GPUs belong to primary, the rig row is absent
and an ordinary model request succeeds. Retain the rig's independent database/key until evidence
is recorded; any later cleanup is separate from restoring service.

## Voice pages and Home summaries

Milestones 88 and 89 were implemented on 2026-10-14. The owner confirmed Home (88) checks
passed on **2026-10-15** and accepted Voice page/playback qualification on **2026-10-16**.
For a new or older installation, the deployment requirements below still apply.
Deploy matching WeightRoom and LoadCoach code for Home's `see-summaries` ability. Grant it
through Groups or an account's abilities to permit group/total summaries and request metadata.
Personal remains the default; request details retain existing log permissions.

Voice pages require matching WeightRoom, voice-gateway and compute voice updates, including
`settings/defaults/voice.toml`. Existing deployers include these packages/defaults and already
build the desktop ZIP. No new service, port, dependency, database migration or credential is
required. WeightRoom's optional `voice_gateway` defaults to the private listener at
`http://127.0.0.1:8446`; override it only when the existing private gateway listens elsewhere.
Do not point it at the desktop or public Twilio listener. Chat → Voice is the browser voice
chat itself, signed in as the person using it, and Chat → Voice desktop app is the download page.
The retired `voice_url` setting is ignored, so a file that still has it starts as before.

Check System → Voice with a `manage-settings` account. Verify status/readiness, saving the
speaker and greeting texts, program uptake, explicit regeneration into the front cache,
voice preview and cached greeting playback. Status and playback must work without waking
compute. Synthesis requires compute to be awake and does not load/hold a language model.
A failed generation must preserve the previous complete cached greetings. Keep existing V7
capacity and V10 browser-account qualification separate from these admin-page checks.

Milestone 100 voice latency diagnostics were deployed and owner-accepted 2026-10-16. For an
update, deliver matching compute `voice`, front `voice-gateway`/WeightRoom and browser/worklet assets; rebuild
the desktop ZIP through the existing packaging/deploy script. New clients report queue clears
only when the gateway advertises `queue_clear_ack`, so older gateways retain their existing
conversation protocol. Older clients continue working but queue-clear latency is unavailable.
No new dependency, port, key, database or migration is needed. Verify private System → Voice
statistics, permission removal, unavailable acknowledgements and status while compute is off.
Gateway's 200-turn/24-hour page window resets on restart; numeric service journals retain
history according to their configured policy. Real audio latency targets and V7 remain separate
qualification gates; no service restart or physical test was performed for this local change.

## Chat sidebar and shared options

Milestone 90 was implemented on 2026-10-14; the owner confirmed all Chat checks passed and
completion on **2026-10-15**, including the independent Options panel refresh correction.
The following deployment/checklist remains applicable to new or older installations.
Deploy updated WeightRoom code, templates and static assets together. The existing front
deployer copies them and reloads WeightRoom. This change needs no compute update, schema
migration, dependency, new service or credential. Verify Chat's section-rail conversation
list, inline rename/trash controls, file selection, shared composer/Ask again Options,
0–2 temperature validation and saved prompt settings after reload on desktop and mobile.
Existing temporary mode and comparison-pair boundaries remain unchanged.

## Database choice

`keep` retains every existing store binding. `sqlite` writes explicit local bindings. `postgresql`
writes bindings to owner-only DSN files; credentials never enter a normal TOML file or the command
line. The installer keeps independent databases for compute core/identity, telemetry, chat,
benchmarks, and plugin data.

Changing an engine is allowed only while the selected stores are empty. The installer deliberately
refuses to point a populated SQLite installation at empty PostgreSQL databases, or the reverse,
because verified cross-engine migration is a separate maintenance operation. This prevents an
installation choice from making existing users, logs, chats, or benchmark results appear lost.

Database P4 is deployed; its migration/recovery workflow passed isolated PostgreSQL 18
qualification on 2026-10-16. An existing populated store uses the separate System → DB
migration wizard described in [the database guide](database-storage.md#p4---offline-engine-migration).
The deployed 2026-10-16 installer-audit correction includes front's `roles.db` alongside
`accounts.db`, chat, benchmarks, plugins, alerts and schedules. Use matching corrected installer
and migration inventory versions before treating an audit or P4 profile as production preflight
evidence.
It requires matching updated openWeightGym code and restarted services on every participating host,
`systemd-run --user`, `systemctl --user`, passwordless SSH for remote hosts, owner-only migration
and acceptance files, an empty target, and compatible native PostgreSQL tools where that engine
is involved. The source owner needs Python's `postgresql` extra and `pg_dump`/`pg_restore`, even
for a SQLite source migrating to PostgreSQL. Provision secret-file references independently
on every host that receives a new binding. The installer still refuses unverified direct switches;
use `--database keep` when installing migration-capable code on an existing machine. P4 has not
switched production stores; live bindings remain SQLite. The existing front deployer copies the
new modules without copying private `settings/migrations/`, keys or runtime data.
WeightRoom and turnstile share the `weightroom.plugins` store binding from the front settings file.
P5 adds storage health and verified local backup age to Machine and System → DB; it was deployed
and live-verified for primary SQLite stores on 2026-10-16, with PostgreSQL 18 signals qualified
in isolation. Install matching LoadCoach and WeightRoom code to show compute signals.
SQLite-only hosts need no PostgreSQL tools for these read-only checks. For PostgreSQL,
review [P5 operations](database-storage.md#p5---operational-postgresql-support) before provisioning:
startup migrations need object ownership, and isolated native restore verification currently
uses the same configured login with CREATEDB and access to the maintenance database. That guide
covers private networking/TLS, grants, connection budgets, vacuum, PITR choices and upgrades.

For an existing PostgreSQL server, provide one protected DSN file per required store:

```bash
scripts/install-suite.sh --services front --database postgresql \
  --dsn chat=$HOME/.config/suite/chat.dsn \
  --dsn benchmarks=$HOME/.config/suite/benchmarks.dsn \
  --dsn plugins=$HOME/.config/suite/plugins.dsn \
  --dsn alerts=$HOME/.config/suite/alerts.dsn \
  --dsn schedules=$HOME/.config/suite/schedules.dsn
```

Each file must be readable only by its owner, normally mode `600`. Interactive installation asks
for missing paths. `--provision-postgresql` installs the packages, creates a local login and one
database per store, generates passwords, and writes DSNs under `keys/postgresql/` with owner-only
permissions.

WeightRoom's P5 alert history uses `weightroom.alerts`, defaulting to `data/alerts.db` when its
binding is omitted. A fresh PostgreSQL front install also requires the `alerts` DSN (or local
provisioning). Existing populated installations retain their engine/bindings with `keep`; adding
this feature creates only its missing repository. Ensure the front program key retains
`mirror-users` for sanitized background collection. Spotter's service account needs journal
read access to observe kernel memory kills; unavailable access appears on the Alerts page and
does not claim recovery. Progression P5 was accepted as complete on 2026-10-14 after live
source/background collection checks and deployed-code temporary-store lifecycle checks.
The 2026-10-14 journal correction was applied by the operator on compute:
it replaces the invalid `journalctl --kernel` argument with `_TRANSPORT=kernel` and confirms
kernel access separately when no memory-kill events match. No additional privileges are granted
by this correction; an inaccessible journal still appears unavailable.

P6 / milestone 71 adds `weightroom.schedules`, defaulting to `data/schedules.db`; fresh PostgreSQL
front installations require a `schedules` DSN or provisioning. Compute's hashed grant table uses
its existing core binding. Existing `keep` installations preserve their selected engines and
create the missing default store on first start. No extra service, port, package or program key
is needed. Deploy matching front/compute code before enabling schedules. An operator's online
sign-in authorizes the particular job; current account/model permissions are checked on dispatch.
Front's schedule database and its recovery archives contain protected scoped job credentials,
like the benchmark database. General API/provider keys retain their existing separate recovery policy.
The table's Edit control needs the matching LoadCoach schedule-edit endpoint and an online
owner sign-in; it rechecks job/model permissions and replaces the scoped grant. Delete requires
an online sign-in to revoke compute authorization, preserving submitted work and history.

`--pgvector` installs the distribution's pgvector package. It can be installed while openWeightGym still
uses SQLite so the host is ready for future vector schemas. The installer does not run `CREATE
EXTENSION vector`; that remains deferred until an openWeightGym schema owns vector data.

## Auditing and automation

### Standalone plugin adapters

System → Plugins generates a private Linux bundle for each configured instance. Its root
`README.md`, beside `install.sh`, covers prerequisites, install options, service/foreground
operation and troubleshooting; the maintained template is
[plugins/INSTALLER-README.md](../plugins/INSTALLER-README.md). Run it as the adapter's normal
login account with Python 3.11 or newer and venv support. This is a separate runtime environment
from an existing Hermes installation. `--no-service` skips systemd setup; `--directory` changes
the default `$HOME/.local/share/suite-adapters/PLUGIN/INSTANCE` location.

The 2026-10-14 bundle fix copies the required `db` package into the install destination and
includes its migrations and schemas in the archive. Older bundles fail wheel setup with
`package directory 'db' does not exist`. Use updated WeightRoom code and download a fresh
bundle for the same instance before retrying. Reinstallation preserves installed settings,
pairing keys and `data/runtime.db`; it does not restart an already running adapter service.
The bundle generator is deployed and a disposable install passed; a fresh install on a separate
adapter host has not been verified. The existing front Hermes adapter was qualified live separately.

### Remote tool hosts

A machine that holds files or repositories for the ToolLocker on compute is set up with
[scripts/install-tool-host.sh](../scripts/install-tool-host.sh) (milestones 73 and 74), run there
as the account that may read them, from a copy of the suite. It needs Python 3.11 or newer, and
git for `--code`; it installs no package. It copies the tool host (`toollocker/__init__.py`,
`toollocker/tools/__init__.py` and `__main__.py`) into `~/.local/share/toollocker-host`, the
suite's `files.py` and `code.py` into the tool folder `~/.local/share/toollocker-host/tools`, and
[toollocker/README.md](../toollocker/README.md), which says how to add a tool, beside it. It keeps
its key in `~/.config/toollocker-host/host.key` (mode 600), writes the tool settings
`~/.config/toollocker-host/tools.toml` (mode 600) the first time from `--files` and
`--code`, and writes the
one systemd user unit `toollocker-host.service` on `--port` (9100 unless given), optionally
bound to one IP with `--bind ADDRESS`. The unit limits memory to 512 MiB and tasks to 32, uses
an owner-only umask and grants no new privileges. `--no-start`
writes it without starting it. It prints the key and the `[servers.NAME]` section to add to
compute's `settings/toollocker.toml`, which are carried by hand, followed by a ToolLocker restart.
It does not change the firewall: admit only compute to the printed port, or use a Tailscale ACL.
`sudo loginctl enable-linger USER` keeps the unit running while nobody is signed in. Running it
again replaces the suite's own files and the unit, and keeps the key, the settings and every
other tool file; once the settings exist it refuses `--files` and `--code`, and folders are
changed in `tools.toml`. On 2026-10-16 the scoped front host was installed on
`10.77.10.42:9100` with a private-interface bind and a harmless fixture folder; its unit
was active and the compute ToolLocker discovered two tools. Authorized read, missing/wrong-key
and browser-origin refusal, outside-folder refusal, per-tool sensitivity, account hiding and
revoked-account access were verified through the live front/compute path. The existing compute
ToolLocker 2 GiB service ceiling was preserved.
[Exact bounded checks](../data/qualification/delivery-2026-10-16.json).

### openWeightGym service audit

Progression P4's native tool-use/recovery benchmarks require matching updated WeightRoom and
LoadCoach code. The existing installer/deployer already includes the `benchmarks` package;
there is no new service, port, dependency or real-tool server. The installed llama-server must
support native OpenAI-style function calls and `/apply-template` with tools. Jinja must be
enabled, and the recorded GGUF template must advertise tool support. Explicit `jinja=false`
or `no-jinja=true` and templates without that capability produce unsupported results rather
than a zero score. The benchmark does not change serving flags to make a model support tools.
P4 was deployed; completion after production runs was confirmed by the owner on 2026-10-14
and removed from the progression backlog. See
[the benchmark procedure](../benchmarks/README.md#tool-use-and-recovery-p4).

Retrieval hardening (milestone 86, now `retrieval-v3`) requires matching front and compute
updates: generated archives exceed the earlier 64-KiB task-body cap. Update LoadCoach's
`tasks.py` before starting new retrieval runs on the updated WeightRoom worker. Authorized
evaluation probe bodies now accept up to 1 MiB; other task bodies keep 64 KiB. The existing
packages/deployers include these files, with no new service, port, dependency or settings.
Current `retrieval-v3` answer-room and representative qualification are recorded in the
[benchmark audit](benchmarks/qualification-2026-10-15.md#representative-results-and-limits--2026-10-17).

Embedder benchmarks (milestone 87) require matching WeightRoom/benchmarks and LoadCoach
catalogue/task/probe updates. The installed llama-server must serve `/v1/embeddings` with a
pooled float vector and complete native `usage.prompt_tokens`. Configure embedding mode and
pooled output; per-token (`pooling=none`) output does not qualify. Probes preserve pooling and
physical batch flags, so their limits can restrict the measured maximum input. No new service,
port, dependency, credential or store is added. Matching delivery and native qualification
passed on this installation; [benchmark contract](../benchmarks/README.md) and
[three-node resource qualification](cpu-embedding-hosts.md#native-cpu-resource-benchmarks-g18) record evidence and limits.

Audit without changing anything:

```bash
scripts/install-suite.sh --check --services compute
```

Preview a full front installation:

```bash
scripts/install-suite.sh --dry-run --services front --database sqlite --no-start
```

For unattended new installation, provide the service set and database decision explicitly:

```bash
scripts/install-suite.sh --non-interactive --yes \
  --services all --database postgresql \
  --install-postgresql --provision-postgresql --pgvector
```

The package installer supports apt, dnf, and pacman. PostgreSQL provisioning also requires a
working local PostgreSQL service and `sudo` access. Selected units are rendered for the checkout
path rather than retaining the repository's example paths. By default, existing configured
services are enabled and restarted; `--no-start` only installs and enables them.

## Paired JSON benchmark delivery (milestone 103)

Current paired JSON protocols and representative native-schema qualification are in the
[benchmark guide](../benchmarks/README.md#paired-json-and-schema-part-7-milestone-103).
On another installation, deliver the compute general conversation
API before starting front paired JSON plans, with matching run-detail and metric-description
templates. Existing question-only requests still work. No dependency,
service, key, port or database migration is added. Completed v2/v3 JSON results retain their
free scores; paused older plans cannot be extended with v4 cases and need a new run. No
results are discarded or reruns queued automatically. Qualify native schema enforcement,
unsupported-format reporting, context coverage, pause/resume and operator-selected models
before calling the new structured path live-verified.


## Private document collections (C08, milestone 105)

Implemented **2026-10-15**, deployed **2026-10-16**. WeightRoom installs the `knowledge` extra,
pinned pypdf 6.20.1, through both front deployment modes and `install-suite.sh` when WeightRoom
is selected. Compute and
plugin runtimes need no parser dependency. Dev installs include it for the fixed PDF parser
checks. The resource-limited PDF child requires Linux; missing support is a visible ingestion
failure, while text ingestion remains available.

Deliver matching LoadCoach first for guarded local inference and its model-catalogue capability,
then WeightRoom/templates/assets. Old compute is refused before collection inference. Startup
applies `weightroom.chat` migration 3 to the existing private store; use existing backups first.
No database binding, credential, service, port or pgvector extension is added. Preserve the
store/migration metadata when rolling code back; older code has no collection handlers.
Deployment and bounded owner grounding qualification passed on 2026-10-16. See
[the limits and operational contract](collections.md).


## Versioned prompts and fixed workflows (C10, milestone 106)

Implemented **2026-10-15**, deployed **2026-10-16**. Deploy matching C08 guarded local inference
and updated WeightRoom code, templates and navigation through the existing workflow. Startup applies `weightroom.chat`
migration 4 in the existing private store; normal backups/transfer cover prompt versions,
plans and attempts. No new dependency, service, port, store, key or agent runtime is added.
Interrupted steps pause until explicitly resumed with current caller authorization; credentials
are not persisted for restart replay. Keep the store/migration metadata on code rollback;
older binaries do not implement workflow handlers. Existing maintenance drain counts accepted
background steps. Deployment and H3 research-to-draft qualification passed on 2026-10-16; read
[the workflow guide](workflows.md) for source, cost and retention boundaries.

## C07 advisory allowance delivery (milestone 107)

Matching front handlers/templates and compute attribution code were present on 2026-10-16;
the live front accounts store has the policy table. Manager editing, restricted access and
retained attribution passed bounded qualification on 2026-10-17;
[coverage limits and receipts](allowances.md#storage-api-and-deployment). On another installation,
deploy updated front handlers/templates and matching compute attribution code. Front startup
applies `loadcoach.users` migration 4 in the existing accounts store; take existing verified
backups first. No dependency, DSN, listener or credential is added. Check Logs → Usage →
Spending allowances with a log reader/account manager and a normal member. Confirm unknown
and stale coverage, rename/key ownership, and that exceeding a soft allowance admits ordinary
requests while existing hosted ceilings still apply. Old compute lacks stable attribution;
do not claim complete coverage from front-only delivery. The legacy `accounts-from` move
refuses populated policies; use verified full-store transfer. Rollback keeps the additive
table/log metadata; earlier code has no allowance UI. See [the contract](allowances.md).

## DB1 ownership and retirement (milestone 124)

The installed split deployment enables `[turnstile] front_tasks = true` and
`front_settings = true` in front's `settings/weightroom.toml`, and root-level
`front_tasks = true` in compute's `settings/loadcoach.toml`. Compatibility defaults remain
false for unconverted installations. Matching code is required on front and compute.
Front IDs remain front-owned if new creation is disabled; after retirement, compute
refuses legacy mode until its pre-retirement database is restored.

For another installation: drain and pause benchmark creation, take verified paired/outside
recovery, deploy matching code, then preview/apply the terminal task/history, grant and
front-program settings imports with `python -m db.consolidation_import`. Enable ownership
only after exact comparison and console checks. The existing services, ports and credentials
are reused. Front-only settings must not be reset through compute's CLI or API.

`python -m db.consolidation_retire --help` describes explicit proof preparation, preview and
apply. Preparation compares the archived compute/front databases, exact approved exceptions
and outside copies. Before apply, stop **LoadCoach, ToolLocker and Spotter**, which share
compute's store. The command refuses journals or changed rows/recovery, preserves live data,
records migration 1 and runs `VACUUM`. Start all previously active units afterward.

`scripts/deploy.sh` skips legacy data imports from a DB1-retired store. The deploy scripts send only a
clean, committed checkout: with any uncommitted or untracked change they stop and list it.
Services normally run the code in their installed folder. Inspect effective unit/drop-ins for
[runtime source overrides](#runtime-source-overrides) before assuming that folder is active.

Before an ownership rollback, disable new front task creation and drain or cancel existing
front tasks; their IDs remain front-owned. Transfer current front-program settings back before
changing their authority. After retirement, stop every shared store writer (LoadCoach,
ToolLocker and Spotter), restore the verified pre-retirement compute copy with compatible
code/settings, then restart and verify forwarding. Never recreate empty legacy tables or
overwrite newer front history as a recovery method.

Native recovery archives remain under `data/backups/SET/` on their respective hosts. The
pre-retirement pair is `20261017T000852Z-df0e3c`; later acceptance and restore checks are
recorded in the [database inventory](database-storage.md#live-database-inventory). Protected
copies, row comparisons and completion receipts are under
`~/.local/share/suite-db-audits/2026-10-16-db1/` on compute and
`~/.local/share/suite-db-audits/2026-10-16-db1-cutover/` on front. Independent compute snapshots
remain under `~/.local/share/suite-retired/2026-10-16/db1-post-cutover/`.

## Node installation and updates

The app's node installation entry point is **Settings → Compute nodes → Make a pairing code**
(superusers only). Run its generated installer command on the GPU machine; the one-time code
joins it to the console. See [Adding a GPU node](installation.md#adding-a-gpu-node).

Use [`owg update` or `owg repair`](installation.md#looking-after-it-owg) on each installed
machine for updates and repairs. Finish active requests, calls and benchmark work before
restarting services. Check [runtime source overrides](#runtime-source-overrides) when a
service runs code from a separate release folder.

Older `[deploy]` settings are ignored and can be removed. The manual maintenance scripts
`scripts/deploy-front.sh` and `scripts/deploy.sh` remain available for developer updates and
the one-time data moves `deploy.sh` performs.

## Console over HTTPS on the LAN

Browsers give a page the microphone only over HTTPS. `scripts/front-https.sh`, run from compute,
serves the console at `https://front.home.arpa` with no outside service: a home CA in `~/ca` on
compute signs a certificate for front (its key never leaves compute, and its name constraint lets
it sign only `*.home.arpa` and LAN addresses), and nginx on front (installed if missing) answers
on the LAN address's port 443 and passes to the console on `127.0.0.1:8440`. Once HTTPS answers,
it sets the console's top-level `host = "127.0.0.1"` and `secure = true` (backup
`weightroom.toml.before-https`); `KEEP_HTTP=1` skips that. Run it again within 30 days of the
certificate's end (825 days) to sign a new one. By hand: a router DNS record for the name, a fixed
address for front, and trusting `~/ca/home-ca.crt` once on each device (the script lists how).

## Front ToolLocker relocation (milestone 128)

Use the existing ToolLocker service on front, behind Turnstile's public port 8443.
Its machine file selects the same account/event database as WeightRoom without a local LoadCoach:

```toml
front = "weightroom.toml"
host = "127.0.0.1"
port = 9443
```

In `weightroom.toml`, set `toollocker_url = "http://127.0.0.1:9443"` under `[turnstile]`,
with `front_settings = true` and port 8443 retained in `ports`. The front service unit must
use the front installation's executable/settings paths; retain `tooldoor.service` as its alias.
`deploy-front.sh` updates configured front ToolLocker instances and restarts their existing
unit. It also refreshes LoadCoach defaults/examples used by an optional front CPU worker, so
new per-node runtime controls appear without replacing that worker's private machine settings.
The default `disable_ram_offload = true` prevents CUDA unified-memory spill. Change it with the
host RAM headroom/limit below that node's GPU inventory on System → Compute nodes; restart that
LoadCoach before new model servers inherit an offload-policy change.
The installer audits the inherited owner store; changing its engine is done through
WeightRoom's binding, never by making a second ToolLocker account store.

Keep each server name, description, level, tool set, tool-level override and filesystem root.
For compute files/code, the live command is the original fixed Python tool command prefixed
with `/usr/bin/ssh -T -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=15
-o ServerAliveCountMax=2 10.77.10.84`. This uses existing front-to-compute SSH access;
no generated/model arguments enter a shell command. Existing HTTP hosts retain their private
bearer-file paths and host-side access rules. Their data and execution stay with their host.

Before cutover, verify the benchmark pause and idle requests, create/restore-check a paired
recovery set, save private code/settings/unit snapshots, and pause front requests through the
shared maintenance barrier. Disable and stop compute's gateway, snapshot its final store,
then use `db.consolidation_import.settings(..., programs={'toollocker'})` and `copy_missing`
for `tool_schemas` against front's migrated `TOOL_SCHEMAS` table. Preview first; compare exact
rows and repeat imports before starting the front gateway. Preserve source records and audit
serialization. Set `front_toollocker = true` in primary LoadCoach's machine file and restart
it when idle: its API/CLI then refuse stale ownership and a compute gateway cannot restart.

After cutover, check signed MCP calls and identical offered schemas, manager tools/settings,
permissions, revocation, trace sensitivity and the audit chain. Check front-owned tools/pages
with LoadCoach unavailable, then gateway outage/refusal/recovery, restart persistence and a
new verified pair. Restore all normal services; retain the owner's benchmark pause.
[Canonical results and receipt locations](database-storage.md#front-toollocker-and-remaining-telemetry-milestone-128).

Rollback: stop the front gateway and prevent new tool/settings requests; preserve current
front schemas, settings and tool events first. Restore the previous routing and unit from
the private recovery copy, transfer any newer settings back to the old owner with exact
conflict checks, then unset `front_toollocker`, restart LoadCoach and enable compute's
ToolLocker. Keep tool events on front's audit chain; never overwrite the whole accounts
store just to move the gateway back. Raw SSH/bearer/machine keys remain separate private
recovery inputs. No physical power/sleep changes or live engine switch are part of this move.


## Identity and credential portability (milestone 152)

`suite-identity` (also `python -m loadcoach.identity_bundle`) is an offline administrator
command for reinstalling at the **same installation/home paths on each host**. It creates
one passphrase-encrypted bundle per host; it does not uninstall anything. The command is
installed with openWeightGym, including `cryptography==50.0.2`. No new port or service is required.
System → Backups links to its source-derived account reference.

**Included:** immutable account IDs, groups, account overrides, password verifiers,
authenticator secrets, API-key verifiers with their original expiry/app restrictions,
spending-allowance policies, hosted-service definitions and front-managed configuration
changes. Credential/configuration files include openWeightGym's `keys/`, top-level service TOMLs,
plugin instance settings, configured `*_key_file`/`*_token_file`/secret/DSN/environment-file
references, installed adapter settings/keys, and the standalone tool-host configuration.
The standard XDG tool-host/adapter locations are discovered. Safe installer-owned
`frontdesk.toml`/`tooldoor.toml` aliases are omitted in favour of their canonical files. The redundant same-directory
`frontdesk.key -> weightroom.key` alias is omitted too; the canonical key is retained.
An actual credential reference through an unsupported symlink causes refusal, not silent omission.

**Excluded:** requests, captures, events, client-access records, chat/voice history,
benchmark results, model definitions/weights, adapter sessions, browser cookie-signing
secrets and runtime CLI tokens. Other services/assets still need installation. Existing
applications retain their plaintext API keys; openWeightGym cannot reconstruct a lost plaintext
key from its verifier. Disabled accounts and key expiry remain unchanged. Since usage
history is absent, historical usage-based spending windows start afresh. Review restored
model/tool allowlists after recreating resources; import never widens them automatically.

**Coverage:** run `inventory` on every host before uninstalling. It prints paths/counts,
never secret values. A missing referenced file fails the inventory/export. Credentials
supplied solely through process environment, custom external launchers, SSH/Tailscale/TLS
identity, or another application's private store are not automatically captured. Select
necessary private files explicitly with repeated `--include /absolute/file` (for example,
a service EnvironmentFile); this copies the file, not the service unit that loads it.
Use `--accounts-config /absolute/weightroom.toml` for a nonstandard front settings filename.
SQLite account stores are supported; PostgreSQL identity import/export and path relocation
are refused rather than presented as qualified. Never restore a front bundle on compute.

```bash
# Run from each host's installation root; choose a different output name for each host.
.venv/bin/suite-identity inventory --root "$PWD"
# Stop that host's openWeightGym services, standalone adapters/tool hosts, key rotators and launchers.
.venv/bin/suite-identity export /media/private/front.identity --root "$PWD" --services-stopped
.venv/bin/suite-identity inspect /media/private/front.identity
```

Enter a separate, strong passphrase of at least 16 UTF-8 bytes at the hidden terminal
prompt. It is never accepted in arguments/environment or stored by openWeightGym. Keep the bundle
outside the installation before uninstalling and retain its passphrase separately. Losing
the passphrase makes recovery impossible. Bundles are versioned, authenticated AES-256-GCM
with fresh 16-byte salt/12-byte nonce and fixed scrypt `N=131072,r=8,p=1` (about 128 MiB
KDF memory). All metadata is encrypted; plaintext is held in process memory. Exports and
restored files use mode 0600; private temporary restore files are flushed and atomically
published. Limits are 32 MiB payload, 1 MiB/file, 4,096 files and 100,000 rows/table.
Cryptographic primitives follow the [PyCA authenticated-encryption](https://cryptography.io/en/latest/hazmat/primitives/aead/)
and [scrypt](https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/#scrypt) APIs.

For the clean reinstall, install openWeightGym with **`--no-start`**, using the same paths and
SQLite bindings. Do not create replacement accounts. Keep services stopped while importing
all host bundles; reinstall standalone adapter/tool-host code without starting it as well.
A bundle restores configuration, not binaries, model assets or systemd units/drop-ins.

```bash
.venv/bin/suite-identity import /media/private/front.identity --root "$PWD"
# Review all paths/conflicts. New installer-generated settings/keys may differ:
.venv/bin/suite-identity import /media/private/front.identity --root "$PWD" --replace-files
# Apply only the reviewed file replacements; different existing accounts are never replaced.
.venv/bin/suite-identity import /media/private/front.identity --root "$PWD" --replace-files --apply --services-stopped
```

An identical repeat import is safe. Different existing accounts, service policies or retained
configuration changes cause refusal, even with `--replace-files`. Imports never merge
identities or re-enable a revoked key in an existing differing account store. The flag permits
replacement of reviewed credential/configuration files only. Restoring an older bundle to an
empty installation restores the older credential state, so export again after any revocation.

`--services-stopped` confirms operator-controlled downtime. A Linux guard also refuses known
same-user openWeightGym processes, but cannot prove that custom/remote launchers or key rotators have
stopped. Keep the filesystem administrator-owned and unchanged during the operation. DB rows
commit in one SQLite transaction; ordinary errors roll back rows and changed files. There is
no atomic transaction across filesystems or machines. A process/power interruption leaves
`.identity-import-incomplete` in the openWeightGym root: **keep services stopped**, inspect the partial
state, then rerun the same bundle. A different bundle is refused while that marker exists;
do not delete it to force another import. Interrupted private `.suite-identity-*` temporary
files may remain in destination directories and must be removed while services remain stopped.
After every host imports, start services and verify an existing client key, account permissions,
node signatures, tools and adapters. Browser users sign in again. Normal recovery archives
continue excluding raw keys; this separate encrypted bundle supplies the private source.
