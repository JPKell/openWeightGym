# openWeightGym

openWeightGym is the project name. Its component packages, commands, API headers and stored identities retain their existing names for compatibility.

The console uses vendored Tabler 1.4 CSS with account Appearance palettes; no Node build or external font/icon service is required. Models use scoped settings and structured editors. General settings are grouped by purpose; Voice and Audio have dedicated settings pages, with voice diagnostics under System → Voice.

openWeightGym provides a web console, an OpenAI-compatible model API, MCP tools and voice conversations.
Start with the [app walkthrough](../app-walkthrough.md) to evaluate each section and record findings.
The [agent walkthrough](agent-walkthrough.md) maps every page to automated checks, live-test boundaries and owner/device evaluation.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/JPKell/openWeightGym/main/install.sh | bash
```

[`install.sh`](../install.sh) checks the machine, asks where it fits (one machine, console only or a
GPU node), fetches Python and a prebuilt [llama.cpp](../scripts/install-llamacpp.sh), and starts
everything; models are downloaded afterwards from the console. Afterwards [`owg`](../scripts/owg) shows status, logs, updates and
repairs; [`uninstall.sh`](../uninstall.sh) removes it, keeping what you choose. The full guide is
[Installation](installation.md); the [project README](../README.md) is the short version.

## Connect

Replace `CONSOLE` with the console machine's address (the installer prints it at the end, and
`owg status` shows it again):

| Use | Address |
|---|---|
| Web console | `http://CONSOLE:8440`, with in-app help under **Docs → Web app** (`/docs/web-app`) (HTTPS once [`scripts/front-https.sh`](../scripts/front-https.sh) is set up) |
| Model API base | `http://CONSOLE:8442/v1` |
| MCP tools | `http://CONSOLE:8443/mcp` or `/mcp/TOOLSET` |
| Browser voice | the voice gateway's page, served over HTTPS |
| Desktop voice | the console, port `8447`; download from `/static/LoadCoach-Voice.zip` |

This project's own deployment addresses are in the [operations log](operations.md#this-deployment).

API clients use a key from **My account → Account**. Browser voice needs HTTPS or localhost;
desktop/LAN API access does not require Tailscale. The signed public telephone route is separate
from the private console and browser voice. Console HTTP is intended for the trusted LAN or VPN.

**Docs → API reference** (`/docs/api`, administrator access) lists implemented OpenAI-compatible
endpoints, compatibility gaps, custom APIs and accepted request headers. Its custom route inventory
is read directly from the installed source; handler details remain in the same page's code reference.

## Use the app

The app opens on **System → Status**. Pages and controls depend on your account's permissions.

| Section | What you can do |
|---|---|
| Chat | Keep conversations, use Mirror Wall image generation and SoundRoom music, sounds and speech, compare models, query private collections and run workflows; open Voice. |
| Models | Configure language/image/audio models, image workflows and roles; install audio packages; manage Files, Hugging Face/Civitai downloads and cloud providers. |
| Benchmark | Start at Runs; filter Results by LLM/Image/Audio, compare Node measurements or embedders and inspect saved evidence. |
| Logs | Inspect usage/costs, requests and traces, batches, reliability, plugin activity and system output. |
| System | Inspect status, telemetry, alerts, schedules, databases, backups and deployment. |
| Settings | Manage compute nodes, users, groups, tools, plugins, voice, audio, image policy and general settings. |
| Docs | Open the administrative system map and source references. |
| My account | Manage your password/API keys, appearance, audio choices and account activity. |

An explicit model name always selects that model. A role such as `chat` selects an eligible
model and explains its choice. `chat:best` prefers quality and can wait; `chat:available` uses
an eligible resident; `chat:fast` estimates time to an answer. Ordinary roles also have a
configurable resident-model preference. Configure these under **Models → Roles**.

**Dry fit** filters LLM, Audio and Image on the selected node without dropping other selected models from the combined preview. Image/audio use fixed measured reservations, with unknown or unqualified memory shown explicitly. It never loads a model.

Idle models stay loaded until their memory is needed. Active work, holds and model locks protect
them from replacement. Dry fit and configuration previews help plan placement; a successful
preview still needs a real load to establish that the configuration works.

### Models and node settings

Open **Models** and choose **LLM**, **Image** or **Audio** above the catalogue table.
**Add a model** is above the table. Choose a cache/storage file and a destination node;
image components and complete audio packages have their own configuration forms. A language file with valid trained-context metadata starts at half that context minimum and the full maximum; both remain editable. New llama.cpp entries use automatic cards and allow splitting; detailed placement stays on their node page.
Installation checks the complete bundle's cache capacity, leaves 1 GiB spare, retains storage
originals and verifies SHA-256 before adding the model. Insufficient space blocks the operation.
Progress and failures appear on Models.

The audio form lists files already in cache/storage, with optional companion files. Download
other compatible weights through **Files → Hugging Face**, then select their runtime family.
Families come from the installed audio.cpp release or the file's embedded contract; a model
need not appear in a preselected download catalogue. The runtime must implement its architecture.

On a model's page, **Compute locations → Add model to node** copies an existing model to
another configured node. Choose a location to edit its CPU/GPU distribution, context,
parallel requests and loading flags. Generation defaults, including reasoning and sampling,
apply across the model's nodes; all configured nodes must answer before those defaults change.
Image/audio engines serve one request at a time and expose no measurable token context.
Their benchmark choice is **Load time**; quality and throughput benchmarks are refused.

**Models → Cloud APIs** shows whether each provider has an API key installed. Use its password
field to install or replace the key; the stored value is never displayed. Only accounts with
`manage-hosted` can change it, and new requests use the replacement without a restart.

### Images

**Chat → Image generation** provides image settings, ordered references, live previews and a
thumbnail browser with a larger selected view. Save reusable settings as a workflow; **Models → Image workflows** selects a named workflow and edits ordinary family-specific fields, preserving every saved version. SDXL workflows include negative prompts; Flux rejects them. The installed **SDXL Base 1.0 · 512 px** workflow uses a bounded, qualified 512×512 profile. Administrators may publish workflows. Generated images and original references are private to the current account
on front until deleted. Downloads contain pixels without embedded prompts or reference metadata.
Authorized temporary/no-log jobs retain assets only in bounded memory for up to one hour.

Under **Models → Images**, configure diffusion weights, encoder, VAE, named LoRAs, upscaler,
one/two-card placement, host RAM cap and workflow bounds. Install the [optional native runtime](operations.md#optional-image-generation-runtime)
on compute first. Files tracks native GGUF, safetensors and PTH components under the `image` place;
this does not execute Python checkpoint loaders. Civitai uses the existing download controls and
verified hashes. **Settings → Image → Allow personal adult-preview opt-in** permits individual users to
enable adult previews; it defaults off and applies immediately. When off, the user control is
hidden and searches enforce safe previews. When on, each user's preference still defaults off.

References support aspect ratios from 1:8 through 8:1. Preparation preserves the originals: downsample large inputs; enlarge small inputs
once using the configured image upscaler, then fit the workflow reference target. A missing or
failed required upscaler fails the job. Output enlargement is separately selected. Masks preserve
the first reference outside edited areas by compositing after generation; they do not imply native
inpainting support. Masks must match the first reference dimensions and preserve its output aspect ratio; white or transparent areas
are replaced. Incompatible or incompletely applied LoRAs fail clearly. Stop cancels active work; Pause finishes the current image before waiting.

The image API accepts JSON generation and JSON/multipart edits at `/v1/images/generations` and
`/v1/images/edits`, returning PNG `b64_json`. `partial_images` follows the compatible 0–3 contract;
openWeightGym's `live_previews: true` streams continuing native previews. Unsupported settings fail clearly.
Use explicit configured model names and ordinary openWeightGym keys. Large jobs share the existing batch
executor; parameter sweeps expand into bounded ordinary image lines. Prepared references are reused
within the same authenticated owner and resident profile (128 MiB/one-hour memory cache); no-log
requests bypass that cache. Load older images pages through retained outputs.

### Audio

Under **Chat**, Collections and Workflows follow Conversations; image and audio tools follow them.
In **Music**, choose a model and read its input guidance. HeartMuLa requires lyrics; use ACE-Step
for an instrumental. Where supported, **Musical direction** supplies tempo, key, time signature
and vocal language. Turbo caps denoising at eight steps and ignores diffusion guidance.

Generate several takes, compare them in the player and choose a favourite. **Reuse settings**
restores its seed, musical controls, source/range or speaker voices; **New variation** keeps the
settings with a fresh random seed and one take. For cover, continuation or repaint, **Use selected
take** avoids downloading and uploading your own audio again. Draft lyrics after choosing the
duration and instrumental setting; the writing helper uses both.

Use **Load older audio** to browse beyond the newest 100 recordings. Tag filtering searches the
whole permitted library. Failed jobs show one status and the retained reason; **Retry** submits
their saved request again, while **Use settings** lets you edit it first. Status polling retries
temporary connection failures. **My account → Audio** sets the initial saved voice for Studio
and Spoken chat. These controls improve direction and reproducibility; musical quality still
depends on the model and prompt.

Select a recording, then drag its waveform or enter **Region start/end** to select a playback
range. **Loop region** repeats that range, including a range that reaches the recording's end;
with no selection it repeats the whole recording. **Clear region** restores whole-recording playback.
Audio playback uses authenticated byte-range requests so seeking works through the console.

### Tools

Use **Settings → Tools** to connect and try tools your account may use. For gateway requirements,
installation on a console-only machine, a custom Python tool and troubleshooting, see
[ToolLocker setup and adding tools](../toollocker/README.md), also in **Docs → Modules → ToolLocker**.

## Where things run

| Component | Responsibility |
|---|---|
| WeightRoom | Front web console, chat, collections/workflows and benchmark worker. |
| Turnstile | Front authentication, model/role placement, hosted calls and signed routing to workers. |
| ToolLocker | Front MCP gateway; file tools execute on their owning hosts. |
| Voice gateway | Front browser/desktop/telephone connections and Hermes integration. |
| LoadCoach | Each worker's model processes, measured admission, inference and scheduling. |
| Spotter | Each host's telemetry and allowed service/power controls. |
| Voice service | Compute-side CPU speech recognition and synthesis. |

The console machine (front) runs WeightRoom, Turnstile, ToolLocker and optionally the voice gateway;
each GPU or CPU worker runs LoadCoach and Spotter. On one machine they run side by side.
Workers appear in **Compute nodes**.
Front owns accounts and retained application history. Workers own model files and local runtime
state. Front-owned pages and saved records remain usable when inference workers are unavailable.
**Telemetry** offers **Wake** on the overview and host detail for nodes with Wake-on-LAN configured
(`wake_mac` and optional `wake_address`, at top level for compute or under `[machines.NAME]`).
Manual wake requires `front-wake`. Previously discovered, permitted deployed models remain selectable
in Chat, Mirror Wall and SoundRoom while a node is unavailable, including after a front restart.
Using a model requests wake on its owning node; `[turnstile].wait` bounds the wake wait (180 seconds
by default), followed by a timeout error if it still does not answer. Wake-on-LAN must also be enabled
on the host and reachable from front. Offline alerts identify unavailable nodes; live management
submissions for those nodes are disabled while front-owned records and retained configuration stay readable.

Model servers use private loopback ports; clients connect through the front addresses above.

## Settings, data and privacy

Settings apply in this order: committed `settings/defaults/NAME.toml`, host-local
`settings/NAME.toml`, then saved web changes. Use **Settings** for front programs and file roots,
and **Compute nodes** for a selected worker's LoadCoach/Spotter policy. Forms indicate restart
requirements. Model cache/storage roots contain `llm`, `encoders`, `adapters`, `projectors`, `image` and `audio`; promotion
copies storage into cache while retaining the library original.

The current deployment uses SQLite; PostgreSQL is supported through the shared storage layer.
Keep host settings, `keys/` and runtime `data/` private. Collection/workflow sources are private
to their owner. Capture/retention depend on policy; permitted no-log work is excluded from retained
request history. Hosted calls require permission, and sensitive/restricted work cannot be offloaded.
Usage distinguishes recorded costs from estimates; advisory allowances are not hard reservations.

## Install, maintain or diagnose

- [Installation](installation.md): the installer, one machine or several, system installations, pairing GPU nodes, `owg`, updating and uninstalling.
- [Operations and release log](operations.md): runtime source overrides, host/service selection, remote tools, identity portability and every release's delivery notes. Editing the checkout alone may not update a running release.
- [Database operations](database-storage.md): store inventory, backups, exports, restore and offline engine migration. Restore requires a maintenance window; raw credentials need separate recovery.
- [Voice](../voice/README.md): clients, modes, addresses and supported capacity; [full specification](voice.md).
- [Benchmarks](../benchmarks/README.md): protocols, scoring, evidence and resource limits.
- [Collections](collections.md), [workflows](workflows.md) and [allowances](allowances.md): feature limits and operating details.
- [Plugins](../plugins/README.md): adapters and configuration; [Hermes](../plugins/hermes/README.md); [tool authoring](../toollocker/README.md).

For a failed reply, start at **Logs → Requests**, then follow its diagnostics/trace and system-log
links. Check **Status** for queue/residency, **Telemetry** for resources and **Alerts** for incidents.
Unavailable readings are not proof of recovery. Service restarts can interrupt requests and calls.

For development, read the [agent reference](agents.md). Personal notes remain in [user_notes.md](user_notes.md).
