# openWeightGym

openWeightGym is the project name. Its component packages, commands, API headers and stored identities retain their existing names for compatibility.

The console uses vendored Tabler 1.4 CSS with account Appearance palettes; no Node build or external font/icon service is required. Models use scoped settings and structured editors. General settings are grouped by purpose; Voice and Audio have dedicated settings pages, with voice diagnostics under System → Voice.

openWeightGym provides a web console, an OpenAI-compatible model API, MCP tools and voice conversations.
Start with the [app walkthrough](../app-walkthrough.md) to evaluate each section and record findings.
The [agent walkthrough](agent-walkthrough.md) maps every page to automated checks, live-test boundaries and owner/device evaluation.

Image extraction: [workspace, API contracts, operating instructions and qualification](image-extraction-plan.md).

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

API clients use a key from **My account → API keys**. Browser voice needs HTTPS or localhost;
desktop/LAN API access does not require Tailscale. The signed public telephone route is separate
from the private console and browser voice. Console HTTP is intended for the trusted LAN or VPN.

**Docs → API reference** (`/docs/api`, administrator access) lists implemented OpenAI-compatible
endpoints, compatibility gaps, custom APIs and accepted request headers. Its custom route inventory
is read directly from the installed source; handler details are under **Docs → Code references**.

## Use the app

The app opens on **System → Status**. Pages and controls depend on your account's permissions.

| Section | What you can do |
|---|---|
| Chat | Keep conversations, use Mirror Wall image generation and SoundRoom music, sounds and speech, compare models; open Voice. |
| Models | Configure language/image/audio models, image workflows and roles; install audio packages; manage Files, Hugging Face/Civitai downloads and cloud providers. |
| Benchmark | Start at Runs; filter Results by LLM/Image/Audio, compare Node measurements or embedders and inspect saved evidence; query private Collections and run Workflows to compare models' grounded answers. |
| Logs | Inspect usage/costs, requests and traces, batches, reliability, plugin activity and system output. |
| System | Inspect status, telemetry, alerts, schedules, databases and backups. |
| Settings | Manage compute nodes, users, groups, tools, plugins, voice, audio, image policy and general settings. |
| Docs | Search permitted guides and references, browse by subject, and open the system map or separate code references. |
| My account | Manage your password/API keys, appearance, audio choices and account activity. |

An explicit model name always selects that model. A role such as `chat` selects an eligible
model and explains its choice. `chat:best` prefers quality and can wait; `chat:available` uses
an eligible resident; `chat:fast` estimates time to an answer. Ordinary roles also have a
configurable resident-model preference. Configure these under **Models → Roles**.

**Dry fit** uses a searchable node-local catalogue beside compact selected rows. Tune exact LLM contexts together, lock values during auto-fit, and include fixed Audio/Image profiles against separate VRAM/system RAM budgets. Save named sets on any answering node, then review and request the complete set through the UI or [node-scoped API](loadcoach.md#validated-model-setup-and-presets-c05-milestone-93). Tuning and saving load nothing.

Idle models stay loaded until their memory is needed. Active work, holds and model locks protect
them from replacement. Dry fit and configuration previews help plan placement; a successful
preview still needs a real load to establish that the configuration works.

### Models and node settings

Open **Models**. The **Compute node** pills show **All** nodes' models or one node's, and
**LLM**, **Image** or **Audio** choose the catalogue table.
**Add a model** is below the table. Tick one or more compute nodes, then choose a
cache/storage file and, for a model that sees pictures, a projector file. Files are listed as
node · cache/storage · file name · size; the full path is in each entry's tooltip. Choosing a
file names the model after it, in lower case and without the extension, until you type a name
of your own. Image components and complete audio packages have their own configuration forms.
A language file with valid trained-context metadata starts at half that context minimum and the
full maximum, and the trained context is noted beneath the form; both remain editable. New
llama.cpp entries use automatic cards and allow splitting; detailed placement stays on their
node page.
Installation checks every ticked node's cache capacity for the complete bundle, leaves 1 GiB
spare, retains storage originals and verifies SHA-256 before adding the model. If any ticked
node refuses, nothing starts. Each node then has its own progress and failures on Models, and a
stored file that several nodes need is promoted into its cache once.

The audio form lists files already in cache/storage, with optional companion files. Download
other compatible weights through **Files → Hugging Face**, then select their runtime family.
Families come from the installed audio.cpp release or the file's embedded contract; a model
need not appear in a preselected download catalogue. The runtime must implement its architecture.

A language model's **Configuration** overview groups settings into compact summary cards.
Select **Edit** to open a group; **Done** returns to its summary without saving. Select node
pills to inspect or edit several copies together. Context, parallel requests, hardware
placement, the model file and loading flags belong to each node. Generation defaults (sampling and reasoning),
the projector, adapters and list position are shared: they appear once and are kept the same on
every copy, with each file named as its own node keeps it. A shared file a node lacks must be
copied there in Files first. **Save** checks and applies each changed node; several are applied
together or not at all, and all configured nodes must answer before shared settings change.
**Additional generation default** offers the other shared options; Advanced loading flags
retains arbitrary runtime flags, inherited node defaults and the installed runtime's flag reference.
Blank values inherit defaults; **Ignore inherited default** explicitly omits a flag for this model.
Request concurrency accepts **-1** for server auto, and a per-request context cap of **0** adds no cap.
Drafts survive group, node and workspace-view switches and status polling. **Discard changes**
restores the accepted configuration, including after a rejected save or loaded preset.

**Compute nodes → Copy or move** copies the complete model to one or more other nodes. Presets,
File & benchmarks, and Maintenance keep their actions in separate views. A move
takes one destination: the copy is verified there before the original is removed, and if the
removal fails the original stays. Image and audio models keep **Compute locations → Add model
to node**.
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

Under **Interact**, image and audio tools follow Text chat. Collections and Workflows are under **Benchmark**, after Compare.
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
temporary connection failures. **My account → Audio** sets the initial saved voice for
Voice generation. Transcription has its own **Interact → Transcribe** page; **Voice chat** opens
the conversational voice client. These controls improve direction and reproducibility; musical quality still
depends on the model and prompt.

Select a recording, then drag its waveform or enter **Region start/end** to select a playback
range. **Loop region** repeats that range, including a range that reaches the recording's end;
with no selection it repeats the whole recording. **Clear region** restores whole-recording playback.
Audio playback uses authenticated byte-range requests so seeking works through the console.

### Tools

Use **Settings → Tools** to connect and try tools your account may use. For gateway requirements,
installation on a console-only machine, a custom Python tool and troubleshooting, see
[ToolLocker setup and adding tools](../toollocker/README.md), also in **Docs → Modules → ToolLocker**.

### My account

The Settings desk uses the existing left menu: **Account**, **API keys**, **Appearance**,
**Audio**, **Workspace**, **Alerts & privacy** and **Logs**. Account lists active sign-ins
with device details where recorded; end another sign-in or all other sign-ins without
revoking application keys. Old sign-ins retain access and show unknown device details.

Appearance saves text size, reduced motion and machine-meter visibility with your account;
the 17 color themes remain specific to each browser. Workspace saves the starting page,
default chat model/role, time zone, date format and clock. Explicit deep links and existing
conversations take precedence, and unavailable defaults never expand model access.
Audio retains unavailable saved choices and adds optional autoplay for new owned output;
playback remains subject to browser permission. Unsaved preference drafts follow menu
switches within the current tab; passwords and keys are excluded from browser draft storage.

Alerts & privacy delivers up to 50 owner-scoped notices from the last seven days for
sign-ins/credential changes, batch/workflow/audio completion and recorded spending at
80% of an applicable 24-hour advisory allowance. The account menu refreshes unread counts
once a minute while visible. Mark all read without deleting audit records. Log retention
is displayed from the deployment policy; other stores retain their own policies.
Export account/audio preferences as JSON, optionally including this browser's saved theme;
exports contain no credentials or conversation/request content. These settings remain on
the control node when inference nodes are offline. No deployment/restart method changes are required.

### My account design previews

Run `.venv/bin/python scripts/preview-account-designs.py`, then open
`build/account-designs/index.html`. The portable page compares **01 Settings desk**
(implemented in the console), **02 Personal overview** and **03 Preferences notebook** in light/dark
and mobile layouts. Its design rationale evaluates proposed sessions, accessibility,
regional formatting, workspace defaults, personal alerts and preference portability.
Blue **Proposed** labels distinguish additions from current settings. Edits and sample
credentials stay in page memory and reset on reload; no live services are contacted.
The preview remains a standalone sample; it never edits real account settings.

### Documentation library

**Docs** (`/docs`) opens the Search-first library: search the current article text, filter by
section, or browse topic cards. Queries are bookmarkable (`/docs?q=…&scope=…`); the form and
article links also work without JavaScript. Every account can search its Web app guides.
Administrator guides, search results and **Code references** (`/docs/code`) require
`manage-settings`. The system map is under **System reference** (`/docs/system`). Article
contents and previous/next links keep navigation local to a section. Source-module links
and older source bookmarks retain their selected module in the separate code browser.

### Documentation design previews

Run `.venv/bin/python scripts/preview-docs-designs.py`, then open
`build/docs-designs/index.html` to compare **01 Search-first library** (selected for the live page),
**02 Guided hub** and **03 Reference desk**. All three use the current docs templates,
repository Markdown and API/source inventories, with full-text search, section filters,
article contents, previous/next navigation, light/dark appearance and mobile browsing.
**Code references** is a separate top-level section; guides link to it instead of appending
source inventories. Article links support browser history and bookmarked section anchors.
Search and reading work offline; explicit live-console links (including source-module
choices) use `--base-url http://CONSOLE:8440` (default `http://localhost:8440`). Regenerate
after documentation edits.

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
state. Pages and saved records on the front remain usable when inference workers are unavailable.
**Telemetry** offers **Wake** on the overview and host detail for nodes with Wake-on-LAN configured
(`wake_mac` and optional `wake_address`, at top level for compute or under `[machines.NAME]`).
Manual wake requires `front-wake`. Previously discovered, permitted deployed models remain selectable
in Chat, Mirror Wall and SoundRoom while a node is unavailable, including after a front restart.
Using a model requests wake on its owning node; `[turnstile].wait` bounds the wake wait (180 seconds
by default), followed by a timeout error if it still does not answer. Wake-on-LAN must also be enabled
on the host and reachable from front. Offline alerts identify unavailable nodes; live management
submissions for those nodes are disabled while records kept on the front and retained configuration stay readable.

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
