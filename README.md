# openWeightGym

Run open-weight models on your own hardware, with a web console, an OpenAI-compatible API, tools
for models over MCP, and voice. One machine or several: a console, and GPU machines that join it.

```bash
curl -fsSL https://raw.githubusercontent.com/JPKell/openWeightGym/main/install.sh | bash
```

The installer checks your machine, asks a few questions (Enter takes the defaults), fetches a
prebuilt [llama.cpp](https://github.com/ggml-org/llama.cpp) for your graphics card and a small
starter model, and finishes with the console open and a model answering. It needs Linux with systemd
(Ubuntu, Debian, Fedora, RHEL and Rocky, openSUSE, Arch, or WSL2) and nothing else installed
first: Python comes with it.

## What you get

- **A web console** for chat, compare runs, private document collections, workflows, image
  generation, music, sound and speech, with accounts, groups and per-model permissions.
- **One OpenAI-compatible API** in front of every model on every machine. Models load when asked for
  and share graphics cards by measured memory; roles such as `chat:fast` pick a model for you.
- **Benchmarks** with saved evidence, comparisons across machines and models, and your own case sets.
- **Tools over MCP** for files and code, on this machine or others, with levels and audit.
- **Voice** in the browser, on the desktop or by phone.
- **Logs** of usage, costs, requests and traces, and **telemetry** for every machine's cards and memory.

## After installing

```bash
owg              # what is running, then a menu
owg status       # programs and addresses
owg logs -f      # follow the logs
owg update       # the latest openWeightGym, settings and data kept
owg doctor       # check everything and say what to fix
owg uninstall    # remove it, choosing what to keep
```

To add a GPU machine, run `owg pair` on the console (or **Settings → Compute nodes → Make a pairing
code**) and run the command it prints on the GPU machine. It joins with a one-time code; no keys to
copy by hand.

`./install.sh --system` installs for the whole machine instead: run by its own account from boot,
with files in `/opt`, `/etc` and `/var/lib`.

## Documentation

- [Installation](docs/installation.md): requirements, choices, ports, pairing, system installations,
  `owg`, the engine, uninstalling, unattended installs and troubleshooting.
- [Guide](docs/README.md): using the console, the API, roles, models and where things run.
- [Operations and release log](docs/operations.md): delivery notes, runtime pins and recovery.
- [LoadCoach](docs/loadcoach.md), [voice](docs/voice.md), [benchmarks](benchmarks/README.md),
  [databases](docs/database-storage.md), [plugins](plugins/README.md), [tools](toollocker/README.md).

## The parts

| Program | Runs on | Does |
|---|---|---|
| WeightRoom | the console | the web console, chat, collections, workflows and benchmarks |
| Turnstile | the console | the way in: keys, accounts, model placement and signed routing to GPU machines |
| LoadCoach | each GPU machine | starts llama-server for each model, schedules requests, shares the cards |
| ToolLocker | the console | the authenticated MCP gateway; tool servers run on their owning hosts |
| Spotter | each GPU machine | telemetry, and the service and power controls it is allowed |
| Voice and voice gateway | GPU machine and console | speech recognition and voices, and the browser, desktop and phone connections |
