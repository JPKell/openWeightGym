# openWeightGym manual walkthrough and findings

**Page inventory reviewed:** 2026-10-30 against [navigation](weightroom/navigation.py), [console routes](weightroom/app.py), [image pages](mirrorwall/ui.py), [audio pages](soundroom/ui.py), [plugin pages](weightroom/plugin_pages.py) and [Docs topics](weightroom/system_docs.py). Automated reviews are recorded separately below; owner checklist results remain unchanged.

Open **[openWeightGym](https://10.77.10.42/)**. Follow the current navigation:
[Home](#home), [Chat](#chat), [Models](#models), [Benchmark](#benchmark), [Logs](#logs),
[System](#system), [Settings](#settings), [Docs](#docs), and [My account](#my-account).
Related detail pages follow their parent page. Also review [Sign-in and recovery](#sign-in-and-recovery),
[connected clients](#connected-clients), and [wrap-up](#wrap-up).
For operation/setup, use the [openWeightGym guide](docs/README.md); runtime/recovery details remain in
[Installation](docs/installation.md#usability-release-2026-10-30).
The [agent walkthrough](docs/agent-walkthrough.md) identifies automated checks and the live-work, maintenance and device boundaries for every page below.

## Automated live review

**Date:** 2026-10-30

The non-administrator `agents` account reached all 25 permitted pages. Seventeen scoped
checks passed: navigation/permissions, scratch Chat and attachments, copying, history/rename,
confirmed deletion, collection originals/privacy, personal API-key revocation, retry,
connection recovery and signed-out destinations. Scratch conversations, collection/source
and API key were removed. Receipts and exact scope: [walkthrough summary](data/qualification/usability/walkthrough/summary.json).

**Automated result: Issue.** After stopping a counting reply, the next prompt reached the
model correctly and streamed, but the model continued counting instead of following it
(request `60898`). That request was stopped and its scratch conversation removed. Stop,
ordinary follow-ups and retry controls worked; the model response needs a separate review.

Retrieval/workflow generation was blocked because loading the available embedder would evict
the existing resident Chat model. Physical audio/devices/clients and subjective acceptance
remain owner checks; administrator edits, optional maintenance and power tests were not
performed in this non-administrator review. The checklist below preserves owner findings.

## Functionality and usability audit

**Date:** 2026-10-30. **Result: Issue — nine reproduced issues; fixes not applied.** Started with Voice, then reviewed the other navigation sections through source, isolated authenticated app/browser checks and existing regression tests. Prior live results above remain separate. P1 means address first because existing data can be lost; P2 means a functional defect; P3 means a usability defect.

### Voice findings

| ID / priority | Reproduction and observed behavior | Recommended change / source |
|---|---|---|
| V01 · P2 | Configure a profile for 0.75× speech and 256 reply tokens; the browser instead selects and submits 1× / 128. A saved-voice profile with a 1,500 ms pause submits 700 ms. Switching between CPU profiles retains the previous speaker rather than the newly selected profile's speaker. | Initialize untouched controls from the selected profile and preserve only explicit caller overrides. [Controls/defaults](voice/static/voice.js), functions `engineControls`, `settingsControls`, `sessionChoices`; [observations](data/qualification/functional-ux-20261030/voice_browser_audit.json). |
| V02 · P2 | Delay `/browser/config`, then press Start. The browser submits only `conversation_mode: direct`, bypassing per-call settings. When settings arrive during that call, newly created controls remain enabled even though changing them cannot affect the active session. | Keep Start unavailable until configuration reaches a usable state; freeze a complete settings snapshot for the call. [Configuration/start lifecycle](voice/static/voice.js), lines 120–188; same observations as V01. |
| V03 · P2 | Offer only Hermes in gateway/profile modes. Direct remains the selected disabled option, the speech-engine selector has no usable profile, and Start shows `Cannot read properties of undefined (reading 'id')`. Selecting Hermes manually recovers. | Select an enabled conversation mode after configuration; handle an empty compatible-profile list with an explanation. [Mode/profile selection](voice/static/voice.js), functions `engineControls`, `sessionChoices` and final configuration handler; same observations as V01. |
| V04 · P2 | Delay the browser session-reservation POST after microphone preparation. Both Start and Hang up remain disabled with Connecting displayed; there is no in-page cancellation during this phase. Hang up becomes available only after the reservation returns and the socket is constructed. | Enable Cancel from the first asynchronous startup step; abort pending work, stop capture and ignore late responses. Preserve the existing cancellation of model preparation after the socket opens. [Startup and cleanup](voice/static/voice.js), functions `close` and `login.onsubmit`; same observations as V01. |

### Findings elsewhere in the app

| ID / priority | Reproduction and observed behavior | Recommended change / source |
|---|---|---|
| U01 · **P1** | In Image generation, select image A, click Delete, press ArrowRight while the confirmation dialog is open, check Yes and confirm. The gallery moves to B behind the modal and the DELETE targets **B**. Reproduced with intercepted requests; no real image was deleted. | Capture the image ID before awaiting confirmation and suspend gallery shortcuts whenever any modal is open. [Deletion/keyboard handlers](mirrorwall/static/images.js), lines 386–387; [receipt](data/qualification/functional-ux-20261030/image-delete.json). |
| U02 · P2 | Hold an image-job submission response for longer than the 1.5-second polling interval. The next state refresh re-enables Generate; a second click sends a second job while the first request is unresolved. | Track submission separately from model/reference availability and keep Generate blocked until it settles. [Submission and refresh](mirrorwall/static/images.js), functions `renderReferences`, `refresh`, `form.onsubmit`; [receipt](data/qualification/functional-ux-20261030/image-submit.json). |
| U03 · P2 | In Speech → Transcribe, start transcription of recording A, then select B before the response arrives. A's words appear beside B's audio and become the current copy/export result. The handler also writes them into B's in-memory metadata. This does not establish incorrect backend persistence. | Capture the submitted recording and request revision; update the visible transcript only if that recording/request is still selected. Guard overlapping transcription submissions too: selecting another recording currently re-enables Transcribe. [Selection/result handling](soundroom/static/speech.js), functions `choose` and the `tr-go` click handler; [receipt](data/qualification/functional-ux-20261030/transcription-selection.json). |
| U04 · P2 | Sort timestamp-only cells across a month boundary. Newest-first puts Jan 31 ahead of Feb 1 because localized labels are compared alphabetically. Affected shapes include Account logs, account/user API-key creation times, request tool events and Voice recent turns. Tables already supplying numeric `data-value` are unaffected. | Sort by the `<time datetime>` instant, or give every timestamp cell a numeric sort value. [Comparator](weightroom/static/sort.js), function `sortTable`; [Account logs](weightroom/pages/account_logs_list.html); [receipt](data/qualification/functional-ux-20261030/date-sort.json). |
| U05 · P3 | Focus the Chat model picker and press ArrowDown twice. The first press focuses the next model; the second does nothing because arrow handling only runs while focus is on the toggle. Tab still reaches other options. | Support Up/Down throughout the listbox, with Home/End and type-to-find for larger model catalogues. [Picker keyboard handler](weightroom/static/chat.js), function `enhanceModelPickers`; [receipt](data/qualification/functional-ux-20261030/chat-picker.json). |

**Recommended order:** U01, V01–V03, U03/U02, V04/U04, then U05. Re-run each reproduction after its fix; a successful reproduction here confirms the defect, not correct application behavior.

### Additional usability improvements

- Voice should link directly to Speech → Voices when Saved voice has no available voices, explaining the required voice, speech model and recognition model before Start is pressed. CPU voices should remain an obvious alternative. This is a source-based recommendation, not an additional reproduced defect.
- System → Voice's **Can start** currently means warmed speech, an already resident conversation model, spare capacity and no lock/drain. Label this **Ready now** and explain loading on use; a cold model alone does not mean the user cannot start a call. [Readiness calculation](voice/service.py), function `ready`; [status labels](weightroom/pages/voice_status.html).

### Coverage and limits

| Area | Checks in this audit |
|---|---|
| Voice | Backend/session authorization, modes, interruption, playback history, hold cleanup, desktop transport, private administration and browser proxies; synthetic browser configuration/startup probes above. |
| Home / Chat | Summary scope/privacy, conversations, controls/restart, attachments, collections/workflow pages and indexing status; picker keyboard probe. |
| Images / Music / Sounds / Speech | Asset privacy, request validation, editing/reuse, loops, pagination and browser controls; delayed image submission, deletion confirmation and transcription-selection probes. |
| Models / Benchmark | Model setup/install, roles, saved sets, node availability, benchmark forms/results/detail navigation, card persistence and narrow layouts. Loads/holds in these tests use isolated fake engines. |
| Logs / System / Settings / My account / Docs | Request filters/diagnostics, usage and log scope, status, schedules, database administration, tools, plugins, users/forms, settings and source Docs. Shared page checks cover navigation destinations and representative detail pages at desktop/phone widths and light/dark appearances. |

The original selected regression runs returned **324 passed, seven failed**. Those seven cases stop at outdated test contracts: Home group links gained `Group:`, Docs summary moved into the page header and mobile navigation moved into Menu, a benchmark selector now matches two headers, and saved-set deletion now requires confirmation. **All seven pass in audit-only copies** using the current contracts; maintained tests remain unchanged. The [adaptation script](data/qualification/functional-ux-20261030/check_current_test_contracts.py) and [Home copy](data/qualification/functional-ux-20261030/test_usage_current_labels.py) retain the exact differences. These tests need maintenance independently of the product fixes above. Five additional browser reproduction checks pass by demonstrating the reported defects, and the Voice probe records six observations grouped into V01–V04. [Validation summary](data/qualification/functional-ux-20261030/validation.json).

Browser reproductions use the actual scripts with synthetic devices/API responses or isolated authenticated app fixtures. [Source fingerprints](data/qualification/functional-ux-20261030/verify_sources.json) confirm the affected Voice, Image, Speech and Chat scripts match the active front releases; WeightRoom and voice gateway were active with zero automatic restarts at inspection. This is not a new authenticated live walkthrough. No live settings, conversations, models, recordings or services were changed. New microphone/speaker, native-client, telephone, real-generation and power acceptance were not attempted; [existing owner Voice acceptance](docs/agent-status.md#voice-acceptance) remains valid. Checklist boxes below remain for the owner's manual review.

Reproduction sources: [Voice](data/qualification/functional-ux-20261030/voice_browser_audit.py) and [media/shared controls](data/qualification/functional-ux-20261030/test_media_audit.py). Evidence under `data/` is local and ignored by Git.

## Before you start

- **Date / session:**
- **Browser / device / window size:**
- **Account / group:**
- **Nodes and models used:**
- **Resume at:**

Tick a check after performing it. Set each area's result to **Pass**, **Issue**, **Blocked**,
or **Not applicable**, and write findings underneath it. An unticked check remains untested.
For an issue, record the steps, expected result, actual result, and relevant page/request ID.
Include usability observations: confusing wording, missing information, awkward controls,
slow updates, and anything that makes you hesitate.

Use a disposable conversation, collection, workflow, user/group and small scratch files where
needed. Keep existing settings before editing them. Checks labelled **Optional maintenance**
can interrupt work or change stored data; defer them until you deliberately choose a quiet
window. The walkthrough does not require a reinstall, live database migration or power test.
Keep passwords, API keys and private document contents out of these notes.

Suggested collection source: save the following as a small UTF-8 text file. Ask both a question
it answers and one it does not answer.

```text
Project Cedar launches on 12 November 2026.
Mira owns the project. Its approved budget is CAD 1,500.
The launch meeting is in Room 4.
```

### Shared page checks

Apply these to every page below, including a representative detail page. Run with the permissions you intend to evaluate; use **Not applicable** for pages that account cannot access. Page entries describe one route/view family, not every saved object ID.

- [ ] Visit each top-level section; check sidebar order, active-page highlighting, detail-page context and browser Back/Forward.
- [ ] Collapse and reopen the sidebar; try a narrow window, keyboard navigation and browser zoom.
- [ ] Try sorting, filtering, pagination and expandable cards wherever offered; check labels, units and readable long values.
- [ ] Watch live updates: they should preserve usable controls, selected text and open details rather than interrupt your work.

- [ ] Check page titles, visible labels, empty tables, missing values and readable button feedback; an asynchronous page is ready only after its initial data has loaded.
- [ ] Check browser-local times with retained UTC values and whole-number counts.
- [ ] Check two-click confirmation for reversible/interrupting actions and an explicit Yes checkbox for data or credential loss; Cancel must leave the item unchanged.
- [ ] Check slow-action feedback and blocked duplicate submissions; a connection-loss notice should remain visible until its failed polling/stream region recovers.

**Result:** Not tested

**Findings / follow-up:**

## Home

[Home](#home-home)

<a id="home-home"></a>

### Home

[Open Home](https://10.77.10.42/).

- [ ] Check that Start a conversation and your permitted models appear before usage. Open a model link and confirm the selected Chat model or appropriate image/audio/collection page.
- [ ] Compare Ready, Busy, Loads on use and unavailable states with Status; missing readings must not be presented as current availability.

**Result:** Not tested

**Findings / follow-up:**

#### Usage and recent activity

- [ ] Check summary cards, usage charts, top models and recent-request links.
- [ ] Change the time range and, when permitted, personal/group/total scope; compare a recent request with Logs.
- [ ] Check empty states and whether the page makes its scope and freshness clear.

**Result:** Not tested

**Findings / follow-up:**

## Chat

[Conversations](#chat-conversations) · [Conversation](#chat-conversation) · [Collections](#chat-collections) · [Collection](#chat-collection) · [Source excerpt](#chat-source-excerpt) · [Workflows](#chat-workflows) · [Workflow](#chat-workflow) · [Image generation](#chat-image-generation) · [Music](#chat-music) · [Sounds](#chat-sounds) · [Speech](#chat-speech) · [Voice](#chat-voice) · [Voice desktop app](#chat-voice-desktop-app)

<a id="chat-conversations"></a>

### Conversations

[Open Conversations](https://10.77.10.42/chat).

- [ ] Before sending, check that the answering-model picker is visible beside Send and advanced controls remain in Options.
- [ ] With an account without permitted models, check the explanation and disabled Send control; follow User guide from the page lead.

**Result:** Not tested

**Findings / follow-up:**

#### History and organization

- [ ] Create a conversation, send a follow-up, leave the page and reopen it; check saved history and ordering.
- [ ] Find a conversation, double-click its title to rename it, and delete only your disposable conversation.
- [ ] Check long titles, many conversations and the narrow-screen sidebar.

**Result:** Not tested

**Findings / follow-up:**

<a id="chat-conversation"></a>

### Conversation

**Page:** `/chat/{ref}`. Open a saved title from [Conversations](https://10.77.10.42/chat); use only your own disposable conversation for edits, Stop and deletion.

#### Replies and controls

- [ ] Send a simple prompt and follow-up; inspect streaming, waiting/loading feedback, reasoning display and final formatting.
- [ ] Try Copy, editing the latest prompt and Ask again; try Continue when an answer ends early.
- [ ] Stop your own in-progress reply; confirm the page settles and another message works.
- [ ] Follow the request link and check the selected model, token counts and timing. Select/copy text while a reply streams.

**Result:** Not tested

**Findings / follow-up:**

#### Options and attachments

- [ ] Change model, system prompt, reasoning and sampling options; check which settings carry into the next message or conversation.
- [ ] Attach a small text file and, with a vision model, an image; inspect previews and whether the reply uses them.
- [ ] Try an unsupported file or a file beyond the displayed limits; check clear feedback and preservation of your typed prompt.
- [ ] In a longer conversation, inspect any context-summary or omitted-message notice and whether the original history remains readable.

**Result:** Not tested

**Findings / follow-up:**

#### Comparison, temporary and plugin modes

- [ ] Compare two permitted models on the same prompt; check separate answers, follow-ups, request links and Stop both.
- [ ] If permitted, try Temporary and check its label and retention behavior against the current logging policy.
- [ ] For an installed plugin model, compare Fresh run with supplied history and Persistent agent session; check continuity and visible errors.

Look closely at model identity and session mode: it should be clear whose answer and history
you are viewing. Skip modes your account cannot use.

**Result:** Not tested

**Findings / follow-up:**

<a id="chat-collections"></a>

### Collections

[Open Collections](https://10.77.10.42/collections).

- [ ] Inspect the collection list, sensitivity, embedder and indexed dimensions; check the empty state and available embedder choices.
- [ ] Create a disposable collection with a permitted ready identity; check required fields and sensitivity limits, then open its detail page.
- [ ] Return through the Chat navigation and confirm the collection remains listed only for its owner.

**Result:** Not tested

**Findings / follow-up:**

<a id="chat-collection"></a>

### Collection

**Page:** `/collections/{collection}`. Open a collection from [Collections](https://10.77.10.42/collections). Inspect original-file downloads and the embedding-space/rebuild disclosure here.

#### Sources and indexing

- [ ] Create a disposable collection with a permitted local embedder; inspect sensitivity and the pinned embedding identity.
- [ ] Upload the sample text and a small selectable-text PDF; download an original and compare it with your source.
- [ ] Start indexing, watch progress, and try cancellation/retry on your test source.
- [ ] Check helpful handling of unsupported/scanned PDFs and displayed file/document limits; uploading alone should not start inference.

**Result:** Not tested

**Findings / follow-up:**

#### Retrieval and answers

- [ ] Search for Cedar's budget; inspect excerpts, source references and retrieval scores, then request a generated answer.
- [ ] Ask for an absent fact, such as Cedar's supplier; inspect whether unrelated evidence or invented answers appear.
- [ ] Adjust the relevance threshold and compare results; similarity is not an answer-confidence score.
- [ ] With a second account, check collection privacy. On disposable data only, test source deletion and embedding-space rebuild/reindexing.

**Result:** Not tested

**Findings / follow-up:**

<a id="chat-source-excerpt"></a>

### Source excerpt

**Page:** `/collections/{collection}/sources/{chunk}`. Follow a citation from a [collection](https://10.77.10.42/collections) or workflow; IDs are selected by the app.

- [ ] Follow a citation from a collection answer or workflow; compare the source name, page/line and excerpt with the uploaded original.
- [ ] Check surrounding text, back navigation and long-line readability; make sure the excerpt belongs to the cited collection.
- [ ] Try the same source link with a second account; private or missing sources must be refused without exposing their text.

**Result:** Not tested

**Findings / follow-up:**

<a id="chat-workflows"></a>

### Workflows

[Open Workflows](https://10.77.10.42/workflows).

#### Create a run and manage prompt versions

- [ ] Create a research-to-draft workflow using your collection; review model choices, output limits and configuration before starting.
- [ ] Save a private prompt, then another version with the same name/kind; check that the earlier version remains available.
- [ ] Inspect the workflow's frozen source references, prompt versions and model identities.

**Result:** Not tested

**Findings / follow-up:**

<a id="chat-workflow"></a>

### Workflow

**Page:** `/workflows/{workflow}`. Open a saved run from [Workflows](https://10.77.10.42/workflows); inspect frozen configuration, source references and prompt versions below its primary actions.

#### Research, review and draft

- [ ] Run research, inspect citations and accept it; start the draft and inspect the review/acceptance steps.
- [ ] Cancel your own step and resume/retry it; leave and reopen the page to check saved state.
- [ ] Inspect attempts, token usage, cost availability and request links; retries should have separate attempt records.
- [ ] Complete the workflow, then delete only the disposable workflow when finished.

**Result:** Not tested

**Findings / follow-up:**

<a id="chat-image-generation"></a>

### Image generation

[Open Image generation](https://10.77.10.42/chat/images).

- [ ] Choose a permitted image model and workflow; inspect family-specific prompt, dimensions, steps, seed and capability/availability guidance.
- [ ] Using a small permitted job, inspect progress, optional live previews, final images, previous/next controls and original-size viewing; stop only your own job.
- [ ] Try supported reference images and saved references; check count/size/aspect limits, strength and adapter controls. Mask wording must distinguish compositing from native inpainting.
- [ ] Reuse settings, download a result and browse older images; confirm deletion applies only to your disposable image. Inspect Temporary only when offered.
- [ ] Follow Workflows to the separate Image workflows page and confirm the selected settings remain understandable on return.

**Result:** Not tested

**Findings / follow-up:**

<a id="chat-music"></a>

### Music

[Open Music](https://10.77.10.42/chat/music).

#### Music and cross-page audio reuse

Follow the [audio workflow](docs/README.md#audio).

- [ ] Compare a few short music takes; check model guidance, duration, lyrics and supported musical controls.
- [ ] Reuse a take and make a new variation; check seed, source/range, loop and speaker-voice restoration where applicable.
- [ ] Browse older audio and filter an older tagged recording; inspect a failed job's reason and retry controls.
- [ ] Open Sounds and Speech; check the saved default voice and transcription/voice-library controls.

**Result:** Not tested

**Findings / follow-up:**

<a id="chat-sounds"></a>

### Sounds

[Open Sounds](https://10.77.10.42/chat/sounds).

- [ ] Choose a sound-capable model and inspect sound-specific prompt/duration/options; music-only controls should not imply unsupported features.
- [ ] Generate a short disposable sound if capacity permits; inspect progress, cancellation, playback, download and a failed-job explanation.
- [ ] Reuse its settings, filter/browse the audio library and check that confirmed deletion targets only the chosen test recording.

**Result:** Not tested

**Findings / follow-up:**

<a id="chat-speech"></a>

### Speech

[Open Speech](https://10.77.10.42/chat/speech).

- [ ] In Studio, choose a speech model/voice; inspect text/script speakers, pauses, supported options, output playback and reuse.
- [ ] Open Transcribe with a short test clip; inspect recognition, optional diarization and result/download controls without claiming model support that is absent.
- [ ] Open Spoken chat and inspect its available voice/conversation controls; distinguish this mode from the separate Voice call page.
- [ ] Open Voices; inspect saved/default voice selection, reference clips, and supported clone/design controls. Use only disposable voices for confirmed deletion.
- [ ] Check older recordings, tags/favourites, job states and personal Audio defaults across tabs.

**Result:** Not tested

**Findings / follow-up:**

<a id="chat-voice"></a>

### Voice

[Open Voice](https://10.77.10.42/voice). Requires the Voice ability. Mark device-dependent checks Blocked when a microphone, output device or configured mode is unavailable.

#### Browser call, audio and interruption

- [ ] Open the private HTTPS web app; check microphone permission, device selection, start/stop and connection feedback.
- [ ] In Direct mode, try a short exchange, follow-up and spoken interruption; evaluate recognition, reply delay, voice quality and echo.
- [ ] If configured, repeat in Hermes mode; check that mode selection and failure messages are clear.
- [ ] Download/extract the desktop app on a device you use, follow its setup instructions and repeat the same exchange.

Record microphone/speaker or headset, chosen mode/model, missed words and perceived delay.
If you use the installed telephone integration, also evaluate a short call and interruption;
record the call separately because its audio path differs.

**Result:** Not tested

**Findings / follow-up:**

<a id="chat-voice-desktop-app"></a>

### Voice desktop app

[Open Voice desktop app](https://10.77.10.42/voice/desktop).

- [ ] Check the download link and platform/setup instructions, including the server address and account/key handling; no private credential should be prefilled in a shared package.
- [ ] Download/extract on a device you use and follow the included instructions; record the platform and app version.
- [ ] Repeat the short exchange and interruption checks from Voice; compare microphone/output choice, errors, reconnection and sign-out.

**Result:** Not tested

**Findings / follow-up:**

## Models

[Models](#models-models) · [Model](#models-model) · [Images](#models-images) · [Image workflows](#models-image-workflows) · [Audio models](#models-audio-models) · [Dry fit](#models-dry-fit) · [Roles](#models-roles) · [Files](#models-files) · [File](#models-file) · [HuggingFace](#models-huggingface) · [Civitai](#models-civitai) · [Fingerprints](#models-fingerprints) · [Cloud APIs](#models-cloud-apis)

<a id="models-models"></a>

### Models

[Open Models](https://10.77.10.42/models).

- [ ] Switch the LLM/Image/Audio pills; check the filtered catalogue first. The Add a model header action should open the initially closed Add form below the list.
- [ ] Choose cache/storage files and a destination node; check promotion/copy progress and an insufficient-space refusal without starting a transfer.
- [ ] Open a model and inspect files, fingerprint, measurements and benchmark links.
- [ ] Try a permitted GPU model and CPU model through Chat; confirm the execution location in its request details.

**Result:** Not tested

**Findings / follow-up:**

<a id="models-model"></a>

### Model

**Page:** `/models/{name:path}`. Open an entry from [Models](https://10.77.10.42/models). Node selection (`?node=…`) and the language/image/audio detail variants belong to this page family.

- [ ] On a disposable entry, use Compute locations → Add model to node; check verified files, duplicate/invalid input handling and sort order.
- [ ] For language, image and audio entries, inspect the appropriate detail view: identity/location/measurements and links to the corresponding editor. Repeat with one installed entry of each type.
- [ ] Check that the selected node is explicit, technical metadata follows primary actions, and missing/offline locations retain readable identity/history.

**Result:** Not tested

**Findings / follow-up:**

#### Settings, previews and presets

- [ ] Configure CPU/GPU placement, context, parallel requests and loading flags on one node; verify another node retains its own values.
- [ ] Change global sampling/reasoning defaults; verify every copy updates, and an unavailable-node refusal leaves the prior settings intact.
- [ ] Preview a small setting change; inspect before/after values, effective flags, context, placement and warnings before applying it.
- [ ] On a test entry, apply a reviewed change, save a named preset and preview/apply it again.
- [ ] Inspect preset scope across related quantizations; confirm the chosen model file, adapters and projector stay appropriate.
- [ ] Preview and test undo on your own change, then confirm the restored configuration. A preview is not proof that a real model loads.

**Result:** Not tested

**Findings / follow-up:**

#### Adapters, vision and embeddings

- [ ] Where installed, inspect adapter/projector association and preview compatibility information.
- [ ] Try a small image prompt with a vision model and a collection search with an embedding model.
- [ ] Check that missing/incompatible companion files or unsupported capabilities produce useful errors.

Use disposable entries for configuration experiments; record the exact model and companion files.

**Result:** Not tested

**Findings / follow-up:**

<a id="models-images"></a>

### Images

[Open Images](https://10.77.10.42/models/images). This is the Image model settings editor; the Image pill on Models is a separate catalogue view.

- [ ] Choose a compute node and an existing image entry, then inspect Add image model without changing a saved entry.
- [ ] Inspect family, required diffusion/encoder/VAE/projector components, optional upscaler and LoRAs; check compatibility guidance, missing-file explanations and cache/storage choices.
- [ ] Review node CPU/GPU placement, split/staging and host-RAM controls separately from shared image defaults and workflow limits.
- [ ] On a disposable entry only, test validation/save/reload; confirm the selected node and files remain correct. Follow Generate images and Image workflows.

**Result:** Not tested

**Findings / follow-up:**

<a id="models-image-workflows"></a>

### Image workflows

[Open Image workflows](https://10.77.10.42/models/images/workflows).

- [ ] Inspect model defaults and saved workflows; switch image models and check compatible controls and limits.
- [ ] Save a disposable workflow, then Save new version; reopen it and verify the earlier version and its exact settings remain available.
- [ ] Apply a version in Image generation; compare model, seed, dimensions, steps, negative prompt and family-specific options. Saved research-to-draft Workflows are a different feature.

**Result:** Not tested

**Findings / follow-up:**

<a id="models-audio-models"></a>

### Audio models

[Open Audio models](https://10.77.10.42/models/audio).

- [ ] Choose a node and inspect existing entries or Add a model; select a complete compatible file/package from cache/storage and inspect optional companions.
- [ ] Check runtime-declared family/kind, licence/commercial-use guidance and supported defaults; missing runtimes or incompatible files need a useful explanation.
- [ ] Review CPU/GPU placement, required exclusivity, host-RAM policy and shared defaults; save/reopen only a disposable entry and verify the selected node.

**Result:** Not tested

**Findings / follow-up:**

<a id="models-dry-fit"></a>

### Dry fit

[Open Dry fit](https://10.77.10.42/dry-fit).

#### Memory and context

- [ ] Switch LLM/Audio/Image pills while preserving node and selected models; confirm fixed memory and unknown/unqualified states, and no audio/image token sliders.

- [ ] Switch nodes, select measured models and move context sliders; check GPU and CPU/host-RAM bars, units and reserved headroom.
- [ ] Try Auto fit and then move a slider manually; inspect the resulting contexts and any over-capacity indication.
- [ ] Check unmeasured models, unavailable telemetry and the role-selection preview; distinguish estimates from measured fit.

**Result:** Not tested

**Findings / follow-up:**

#### Saved sets and holds

- [ ] Save/reselect a disposable set and preview its configuration and placement changes.
- [ ] In a quiet window, apply/load/hold a small fitting set; check member status, hold expiry, renewal and release.
- [ ] Preview undo, restore your changed settings and remove the disposable saved set.

**Result:** Not tested

**Findings / follow-up:**

<a id="models-roles"></a>

### Roles

[Open Roles](https://10.77.10.42/roles).

- [ ] Inspect a role's candidates, required capabilities, minimum context and explanation of what it would pick now.
- [ ] Review benchmark weights, missing/stale evidence rules and loaded-model bonuses; try edits on a disposable role.
- [ ] Request that role through Chat or your API client; compare the actual model and selection reason with the preview.
- [ ] Check that models unavailable to your test account are excluded; remove only the disposable role.

**Result:** Not tested

**Findings / follow-up:**

<a id="models-files"></a>

### Files

[Open Files](https://10.77.10.42/files).

#### Discovery and storage

- [ ] Inspect front and node cache/storage roots; check file type, size, usage, duplicates and missing-file states.
- [ ] Open a file's metadata; inspect available model/adapter/projector actions.
- [ ] With a small scratch file, test copy, move and distribution to a chosen node; inspect destination, progress and final state.
- [ ] Promote a scratch file from storage to cache and confirm the storage original remains.

**Result:** Not tested

**Findings / follow-up:**

#### Optional maintenance: cleanup controls

**Optional maintenance.** Inspect Clear cache, Cleanup nodes and permanent-delete wording
before deciding whether to execute them. Cleanup can continue in the background and be slow
while large files are read or verified.

- [ ] Check that the selected machine/root, protected files and expected effect are understandable.
- [ ] If testing cleanup, inventory every configured node first; files used by any node must remain protected, and an unavailable usage report must block cleanup.
- [ ] Follow background progress through refresh/reopening; inspect failures and final retained files. Limit permanent-delete testing to a scratch file.

**Result:** Not tested

**Findings / follow-up:**

<a id="models-file"></a>

### File

**Page:** `/file?path={path}`. Open a row from [Files](https://10.77.10.42/files); retain any node query supplied by that link.

- [ ] Open a file from Files; verify its full path, selected machine/root, size, metadata and recorded use.
- [ ] Inspect available model, adapter and projector actions for that file type; follow a relevant model link and return without changing its association.
- [ ] If testing copy/move or permanent deletion, use a scratch file only and the Files maintenance guidance. An in-use file must remain protected.

**Result:** Not tested

**Findings / follow-up:**

<a id="models-huggingface"></a>

### HuggingFace

[Open HuggingFace](https://10.77.10.42/hub).

- [ ] Search/open a repository and inspect filenames, sizes, selection and download destination.
- [ ] If you want a download test, choose a small permitted file; inspect progress, completion and its appearance in Files.
- [ ] Check feedback for a nonexistent/private repository, a colliding filename or an unavailable destination.

**Result:** Not tested

**Findings / follow-up:**

**Related view:** `/downloads` is the embedded progress fragment used here, not another framed page.

- [ ] Inspect the embedded download list while a deliberately chosen small transfer runs; check stop/continue/discard and final visibility in Files.
- [ ] Check that transfer progress survives leaving/reopening HuggingFace and does not imply a model is installed or loaded.

**Result:** Not tested

**Findings / follow-up:**

<a id="models-civitai"></a>

### Civitai

[Open Civitai](https://10.77.10.42/civitai).

- [ ] Search or open a source; inspect model/version/file details and cache/storage destinations without starting an unintended download.
- [ ] Check safe-preview behavior and the separate personal opt-in versus global administrator image policy; unavailable or blocked previews need a clear reason.
- [ ] For a deliberately selected small compatible file, inspect transfer progress and find it in Files; check missing/private sources and invalid destinations.

**Result:** Not tested

**Findings / follow-up:**

<a id="models-fingerprints"></a>

### Fingerprints

[Open Fingerprints](https://10.77.10.42/fingerprints).

- [ ] Find a model and inspect recorded file/projector hashes and settings snapshots.
- [ ] Follow links from model/request/benchmark pages and check that they identify the same recorded configuration.
- [ ] After a test-entry setting change, compare current and historical evidence; old results should keep their original identity.

**Result:** Not tested

**Findings / follow-up:**

<a id="models-cloud-apis"></a>

### Cloud APIs

[Open Cloud APIs](https://10.77.10.42/cloud-apis).

- [ ] Inspect configured providers, offered model names, pricing and recorded daily spending ceilings.
- [ ] Check each provider's installed-key status and install/update password field; the stored key must remain hidden, including after a failed submission.
- [ ] Check provider model discovery and mapping without exposing the provider key.
- [ ] If already configured and you choose a paid test, send one short permitted request; inspect source, model and recorded cost.
- [ ] Using a restricted test account/client, verify hosted/sensitivity restrictions produce a clear refusal.

Skip paid inference if you do not want a provider charge; an empty configuration is also worth evaluating.

**Result:** Not tested

**Findings / follow-up:**

## Benchmark

Use one small model and one selected suite for an initial run. Review retained evidence without rerunning the full catalogue; preserve the previous benchmark pause state.

[Runs](#benchmark-runs) · [Run detail](#benchmark-run-detail) · [Results](#benchmark-results) · [Node measurements](#benchmark-node-measurements) · [Embedders](#benchmark-embedders) · [Compare](#benchmark-compare) · [Manage](#benchmark-manage)

<a id="benchmark-runs"></a>

### Runs

[Open Runs](https://10.77.10.42/benchmarks/runs).

- [ ] Queue one selected benchmark; follow queued/running/completed state, sample progress, logs and history.
- [ ] Confirm the Benchmark menu and `/benchmarks` start at Runs; Overview is absent.
- [ ] Check that the run model table and Queue outstanding benchmarks selector exclude image/audio models, including for Load time.
- [ ] Cancel only your test run; check retained completed samples and the resulting state.
- [ ] While your benchmark runs, send a normal Chat prompt and inspect foreground responsiveness and benchmark interruption/retry behavior.

**Result:** Not tested

**Findings / follow-up:**

<a id="benchmark-run-detail"></a>

### Run detail

**Page:** `/benchmarks/run/{number}`. Open a history/run link from [Runs](https://10.77.10.42/benchmarks/runs) or a result table.

- [ ] Inspect run identity: model fingerprint, node, engine, configuration, suite/protocol and timestamps.
- [ ] Inspect state/progress, samples, score components, usage and request/log links; incomplete, failed, cancelled and output-capped evidence must remain distinct.
- [ ] Open a completed sample and compare its recorded answer with the check/result; retained samples should survive cancellation of your own test run.
- [ ] Use links from Results/Node measurements/Compare and confirm they open this same recorded run rather than a newer configuration.

**Result:** Not tested

**Findings / follow-up:**

<a id="benchmark-results"></a>

### Results

[Open Results](https://10.77.10.42/benchmarks/results).

- [ ] Switch LLM, Image and Audio pills; check that model results and run links stay within the selected type, and image/audio offer Load time only.
- [ ] Confirm the Scores grid contains language models only; saved image/audio load evidence remains accessible separately.
- [ ] Change the selected benchmark; confirm the model-type selection remains active and node comparisons are on their separate page.
- [ ] Sort the quality heatmap by a benchmark; check that chart/table ordering agrees and unmeasured entries remain clear.
- [ ] Open sample/evidence details; compare a recorded answer with its score and inspect unsupported, failed or output-capped cases.
- [ ] Check current versus historical/settings-changed evidence. Heatmap colors scale per benchmark, so inspect the actual score and coverage too.

Review whichever installed suites matter to you: extraction, arithmetic, retrieval/code retrieval,
instruction hierarchy, reasoning, multi-turn consistency, tables, abstention, JSON/schema,
instruction following and tool-use/recovery.

**Result:** Not tested

**Findings / follow-up:**

<a id="benchmark-node-measurements"></a>

### Node measurements

[Open Node measurements](https://10.77.10.42/benchmarks/node-measurements).

- [ ] Switch LLM, Embedders, Image and Audio pills, then the available measurement pills; inspect node columns and type-specific units.
- [ ] Select Speed and switch between LLM and Embedders; Speed should remain selected while the models and measurement change to the selected type.
- [ ] Check units and saved-run links; unavailable measurements should be distinguishable from zero.

**Result:** Not tested

**Findings / follow-up:**

<a id="benchmark-embedders"></a>

### Embedders

[Open Embedders](https://10.77.10.42/benchmarks/embedders).

- [ ] Switch Context, Speed and Load time; inspect node columns and CPU/GPU location labels.
- [ ] Follow a result and its comparison link; check input capacity, vector completeness and timing units where recorded.
- [ ] Confirm generation and embedding results remain distinguishable when changing selections.

**Result:** Not tested

**Findings / follow-up:**

<a id="benchmark-compare"></a>

### Compare

[Open Compare](https://10.77.10.42/benchmarks/compare).

- [ ] Select several models and benchmarks; change saved fingerprints/engines/machines and update the comparison.
- [ ] Check missing/stale evidence labels, score direction and links back to the exact recorded run.
- [ ] Evaluate whether the comparison helps you choose a model for your actual workload.

**Result:** Not tested

**Findings / follow-up:**

<a id="benchmark-manage"></a>

### Manage

[Open Manage](https://10.77.10.42/benchmarks/manage).

- [ ] Review benchmark enablement, formulas, score direction and model-server selection; keep original values before edits.
- [ ] Download the example, import a small disposable versioned case set and inspect/export it.
- [ ] Try invalid JSON or an unsupported check; inspect validation and preservation of your input.
- [ ] If you run the imported set, check the recorded dataset version. Any formula/enablement changes should be restored afterward.

**Result:** Not tested

**Findings / follow-up:**

## Logs

[Usage](#logs-usage) · [Spending allowances](#logs-spending-allowances) · [Requests](#logs-requests) · [Request detail](#logs-request-detail) · [Batches](#logs-batches) · [AI Logs](#logs-ai-logs) · [Log entry](#logs-log-entry) · [Reliability](#logs-reliability) · [Plugin logs](#logs-plugin-logs) · [System logs](#logs-system-logs)

<a id="logs-usage"></a>

### Usage

[Open Usage](https://10.77.10.42/usage).

- [ ] Filter dates, user/group, model, source and outcome; check totals against a known recent request.
- [ ] Switch chart views, daily/cumulative presentation and exact tables; inspect timezone boundaries and input/output totals.
- [ ] Change mock rates and inspect recorded versus model-rate/mock estimates, missing usage and the bookmarkable URL.
- [ ] With a restricted account, confirm only permitted history is visible. Unlogged requests and isolated benchmark probes are excluded.

**Result:** Not tested

**Findings / follow-up:**

<a id="logs-spending-allowances"></a>

### Spending allowances

[Open Spending allowances](https://10.77.10.42/allowances). Open from Usage when available to your account.

[Open Spending allowances](https://10.77.10.42/allowances), also linked from Usage.

- [ ] Inspect user/group/application allowances, recorded amounts, period and coverage/missing-cost notices.
- [ ] If permitted, change a disposable target's allowance and confirm persistence; restore the original policy afterward.
- [ ] Check wording: these allowances are soft spending policy, and the display should distinguish recorded spend from estimates.

**Result:** Not tested

**Findings / follow-up:**

<a id="logs-requests"></a>

### Requests

[Open Requests](https://10.77.10.42/requests).

#### Request table and client access

- [ ] Find a request you just made; try per-column choices/ranges, sorting and pagination.
- [ ] Combine filters, bookmark/reload the URL and clear them; check totals and rejected invalid ranges.
- [ ] Expand Client access; inspect IP, account, first/last seen and accepted/rejected attempts.
- [ ] Compare attribution from browser and API clients; distinguish the originating client from internal forwarding hosts.

**Result:** Not tested

**Findings / follow-up:**

#### Trace search, export and metrics

- [ ] Search a known ingress ID or distributed trace ID; inspect service, machine, application, outcome and time filters.
- [ ] Follow the parent timeline from ingress through routing/execution; check retries and unconfirmed/missing evidence labels.
- [ ] Export metadata and, with an ingress ID, a failure receipt; check correspondence to the selected call and absence of credentials/content.
- [ ] Inspect operational counts, p50/p95, errors/cancellations, source freshness and forwarding backlog; distinguish ingress from internal-operation totals.

Older requests may lack the newer trace evidence. Record the request date and ID rather than
treating missing historical measurements as a newly failed operation.

**Result:** Not tested

**Findings / follow-up:**

<a id="logs-request-detail"></a>

### Request detail

**Page:** `/requests/{number:int}`. Open an ID from [Requests](https://10.77.10.42/requests), Chat, a workflow or benchmark. Live/completed states are variants of this page; background `?part=live` refreshes are not separate pages.

- [ ] Open a completed request and inspect caller/model/location, outcome, timing phases and captured content when permitted.
- [ ] Follow related events, trace, service output and GPU/memory journal links.
- [ ] Compare a successful request with your own cancelled or failed request; unavailable measurements should be explicit.
- [ ] If feedback is offered, record it on a test request and check persistence. Inspect current-request cancellation using only your own work.

**Result:** Not tested

**Findings / follow-up:**

<a id="logs-batches"></a>

### Batches

[Open Batches](https://10.77.10.42/batches).

- [ ] Use the Handing one in examples with an API client to submit a tiny batch; this page monitors batches rather than uploading them. Follow per-item progress, results and errors.
- [ ] Leave/reopen the page and inspect retained state, request links and output download where offered.
- [ ] Cancel only your disposable batch; check completed output and queued/in-progress handling. Inspect Stop all without affecting other work.

**Result:** Not tested

**Findings / follow-up:**

The `/batches/{number}` links return batch files/results, not a separate HTML page; evaluate their contents and download names from here.

<a id="logs-ai-logs"></a>

### AI Logs

[Open AI Logs](https://10.77.10.42/log).

- [ ] Filter events by available user/group/model/event/trace controls and change pages.
- [ ] Locate a known action and follow its request or machine link; check timestamps and event meaning.
- [ ] Check empty results, long values and permitted visibility from a restricted account.

**Result:** Not tested

**Findings / follow-up:**

<a id="logs-log-entry"></a>

### Log entry

**Page:** `/log/{number:int}`. Open an event link from [AI Logs](https://10.77.10.42/log).

- [ ] Open a non-request event from AI Logs and inspect its kind/action, attribution, related events and permitted diagnostics.
- [ ] For a model failure, inspect retained server output and the optional Ask a model helper without sending private output to an unintended model.
- [ ] A request event should redirect to the canonical Request detail page. Missing or forbidden entries must return the appropriate recovery/refusal page.

**Result:** Not tested

**Findings / follow-up:**

<a id="logs-reliability"></a>

### Reliability

[Open Reliability](https://10.77.10.42/reliability).

- [ ] Inspect model/role filters, charts, exact evidence and links to reliability events.
- [ ] Check that failures, exclusions and recovery state have understandable reasons and dates.
- [ ] **Optional maintenance:** try reset/recovery only on a deliberately chosen test target; inspect the resulting evidence and eligibility.

**Result:** Not tested

**Findings / follow-up:**

<a id="logs-plugin-logs"></a>

### Plugin logs

[Open Plugin logs](https://10.77.10.42/plugins/activity).

- [ ] Locate activity from your plugin conversation; inspect instance/model, outcome, timing and available filters.
- [ ] Check error detail and correlation with Requests without exposing private prompts or credentials beyond your permissions.

**Result:** Not tested

**Findings / follow-up:**

<a id="logs-system-logs"></a>

### System logs

[Open System logs](https://10.77.10.42/output).

- [ ] Select available services, inspect recent output and use the time/context controls.
- [ ] Follow an around-this-request link from diagnostics and check whether the chosen time/service is useful.
- [ ] Check unavailable-service feedback and whether sensitive output is appropriately protected.

**Result:** Not tested

**Findings / follow-up:**

Reading service output requires `restart-services`; an account without it should have no System logs link and should receive a persistent Access denied page on a direct visit.

## System

[Status](#system-status) · [Telemetry](#system-telemetry) · [Physical host telemetry](#system-physical-host-telemetry) · [Machine](#system-machine) · [Voice](#system-voice) · [Alerts](#system-alerts) · [Schedules](#system-schedules) · [DB](#system-db) · [Database migration preview](#system-database-migration-preview) · [Backups](#system-backups) · [Restore preview](#system-restore-preview) · [Deploy](#system-deploy)

<a id="system-status"></a>

### Status

[Open Status](https://10.77.10.42/status).

- [ ] Inspect every configured node, resident model, CPU/GPU placement, memory, active request and queue; compare with your Chat activity.
- [ ] Check live updates and stale/unavailable labels; inspect the node selector before any control action.
- [ ] In a quiet window, test a small model's lock/unlock and unload behavior; active work and holds should remain protected.
- [ ] **Optional maintenance:** test draining/resuming or clearing only deliberately selected test work; restore request acceptance afterward.

**Result:** Not tested

**Findings / follow-up:**

<a id="system-telemetry"></a>

### Telemetry

[Open Telemetry](https://10.77.10.42/telemetry).

- [ ] Inspect host cards and open each physical host's charts; change time range and examine CPU, RAM, GPU/VRAM and other available readings.
- [ ] Check that LoadCoach instances on the same physical host are grouped correctly and resident-model/freshness labels make sense.
- [ ] Compare the compact top meters with the full page; check missing data, units, chart tooltips and narrow-screen readability.

**Result:** Not tested

**Findings / follow-up:**

<a id="system-physical-host-telemetry"></a>

### Physical host telemetry

**Page:** `/telemetry/{fingerprint}`. Select a host from [Telemetry](https://10.77.10.42/telemetry); repeat for each configured physical host.

- [ ] Open a physical host from Telemetry; verify hostname, grouped compute instances, resident models and retained readings match that host.
- [ ] Change Hours shown/Until, inspect chart zoom/tooltips and compare browser-local axis times with the retained UTC value; check CPU, RAM, GPU and storage units.
- [ ] For an already unavailable host, retained charts should remain readable while dependent controls explain their availability. Inspect Wake visibility/disabled guidance without waking a host.

**Result:** Not tested

**Findings / follow-up:**

<a id="system-machine"></a>

### Machine

[Open Machine](https://10.77.10.42/machine). This service/power view is linked from the compute host detail rather than a separate top-level navigation item.

Inspect the [machine controls](https://10.77.10.42/machine) alongside Telemetry.

- [ ] Review allowed services, current state, available actions and explanations of draining/idle policy.
- [ ] Check that power actions identify the affected host and make interruption clear.
- [ ] **Optional maintenance:** evaluate service restarts or sleep/wake/reboot/shutdown only as a separately chosen maintenance exercise; record recovery and resumed availability.

**Result:** Not tested

**Findings / follow-up:**

<a id="system-voice"></a>

### Voice

[Open Voice](https://10.77.10.42/system/voice/status). This is System → Voice diagnostics. Configuration lives separately under Settings → Voice.

- [ ] Inspect service readiness, supported modes and recent voice-turn timings after your voice test.
- [ ] Follow Voice settings to preview a voice or inspect recorded greetings; these controls belong to Settings → Voice, not this diagnostics page.
- [ ] Compare recognition, model and synthesis timing with your perceived delay; unavailable stages should remain distinguishable.

**Result:** Not tested

**Findings / follow-up:**

<a id="system-alerts"></a>

### Alerts

[Open Alerts](https://10.77.10.42/alerts).

- [ ] Inspect open/resolved episodes, severity, source freshness and detail links; check filters/pagination where offered.
- [ ] Acknowledge an appropriate existing/test episode; check that acknowledgement and resolution have distinct meanings.
- [ ] Review memory-incident and observation-loss/backlog presentation if evidence exists; inspect configured thresholds without creating a production failure.

An empty alert list does not exercise the alert lifecycle. Mark unavailable scenarios untested;
error-rate/latency thresholds may intentionally be disabled until baselines are established.

**Result:** Not tested

**Findings / follow-up:**

<a id="system-schedules"></a>

### Schedules

[Open Schedules](https://10.77.10.42/schedules).

- [ ] Create/edit a disposable backup, fixed benchmark or one missing/outdated pair schedule; select its preferred node and scopes; inspect weekdays, time, timezone, missed-run policy and next occurrence.
- [ ] Pause/resume it and inspect saved state; if you choose to let one occurrence run, follow its linked work and history.
- [ ] Check invalid times/timezones and ownership restrictions; remove the disposable schedule so it does not run unexpectedly later.

Look closely at local time versus UTC. Schedules do not wake compute; unavailable-node and
overlap states should remain understandable.

**Result:** Not tested

**Findings / follow-up:**

<a id="system-db"></a>

### DB

[Open DB](https://10.77.10.42/databases).

- [ ] Inspect machine/store inventory, engine, table counts/sizes, schema details, health and verified-backup age.
- [ ] Try a complete database or table SQL export on suitable test data; inspect its scope and content without sharing private records.
- [ ] Review installed export tools and migration-readiness messages; the current deployment uses SQLite.
- [ ] If a migration profile exists, inspect Check and preview. A live engine migration remains a separate maintenance decision.

**Result:** Not tested

**Findings / follow-up:**

<a id="system-database-migration-preview"></a>

### Database migration preview

**Page:** `POST /databases/migrations/{profile}/preview`. Open Preview from [DB](https://10.77.10.42/databases); there is no standalone GET link. A live engine change is not part of this routine walkthrough.

- [ ] With an existing isolated migration profile, inspect preflight, source/target binding, required tools/space, event-chain/artifact checks and proposed configuration changes.
- [ ] Review every affected host/service, backup/recovery behavior and validation report; a preview must not be mistaken for a completed migration.
- [ ] **Optional maintenance:** Start reviewed migration changes engines and interrupts services. Use only a separately chosen maintenance exercise with recovery prepared; review the durable report back on DB afterward.

**Result:** Not tested

**Findings / follow-up:**

<a id="system-backups"></a>

### Backups

[Open Backups](https://10.77.10.42/backups).

#### Recovery-set inventory

- [ ] Inspect last verified recovery point, recovery sets, front/compute inventory, verification state and archive/data sizes.
- [ ] If you want a backup test, use Back up both machines and check that both parts finish verified.
- [ ] Open Restore preview; inspect exact files/stores, owning services and recovery instructions. Preview does not perform a restore.
- [ ] Review retention semantics: zero keeps every set. Test an actual restore only in a deliberate maintenance/recovery exercise.

**Result:** Not tested

**Findings / follow-up:**

#### Optional maintenance: identity portability

- [ ] Open the clean-reinstall identity export/import reference; check that host coverage, exclusions and required service stops are understandable.
- [ ] Distinguish encrypted identity/configuration bundles from history backups; this feature uses the terminal command `suite-identity`.
- [ ] **Optional maintenance:** follow the [offline portability procedure](docs/installation.md#identity-and-credential-portability-milestone-152) for an isolated export/import-preview exercise; keep bundles and passphrases private.

An import that changes the current installation, or the planned clean reinstall, is a separate
maintenance exercise. Neither is needed to complete the ordinary page walkthrough.

**Result:** Not tested

**Findings / follow-up:**

<a id="system-restore-preview"></a>

### Restore preview

**Page:** `/backups/{backup_id}`. Use Restore preview for a set in [Backups](https://10.77.10.42/backups); preview itself does not restore data.

- [ ] Open a retained recovery set and compare its ID, front/compute parts, verification state, archived files/stores and excluded items.
- [ ] Review the exact owning services, restore order and recovery instructions; missing/unavailable halves must remain explicit.
- [ ] Return to Backups without restoring anything. Actual restore and clean reinstall belong to Optional maintenance with their own recovery evidence.

**Result:** Not tested

**Findings / follow-up:**

<a id="system-deploy"></a>

### Deploy

[Open Deploy](https://10.77.10.42/deploy).

- [ ] With superuser access, inspect Source on front, machine revisions, reachability, services and job output; compare the effective runtime pin with the release record in Installation.
- [ ] Review pending/running/success/failure presentation and whether source checkout versus active deployed release are distinguishable; inspect another job without starting one.
- [ ] **Optional maintenance:** Pull, Set up, Deploy, Restart services and Add and set up can change code or interrupt work. Use only after affected work drains, preserve pause/source/settings and recovery evidence, and verify the selected machine afterward.

**Result:** Not tested

**Findings / follow-up:**

## Settings

[Compute nodes](#settings-compute-nodes) · [Users](#settings-users) · [User](#settings-user) · [Groups](#settings-groups) · [Group](#settings-group) · [Tools](#settings-tools) · [Plugins](#settings-plugins) · [Plugin](#settings-plugin) · [New plugin instance](#settings-new-plugin-instance) · [Plugin instance](#settings-plugin-instance) · [Voice](#settings-voice) · [Audio](#settings-audio) · [Image](#settings-image) · [General](#settings-general)

<a id="settings-compute-nodes"></a>

### Compute nodes

[Open Compute nodes](https://10.77.10.42/compute-nodes).

- [ ] Switch every configured node; inspect availability, GPU enablement, engine settings, CPU RAM policy and LoadCoach/Spotter settings.
- [ ] Make one suitable reversible test change, save/reload and confirm it affects only the selected node; restore it afterward.
- [ ] Review machine-key rotation status/schedule and recovery guidance.
- [ ] **Optional maintenance:** test rotation or engine/service changes only when prepared to verify all affected node connections afterward.

**Result:** Not tested

**Findings / follow-up:**

<a id="settings-users"></a>

### Users

[Open Users](https://10.77.10.42/users).

- [ ] Inspect the account table, filters, sort/pagination and enabled/disabled state; open a user without changing it.
- [ ] On a disposable account, inspect the Add form, validation and group selection; the chosen group and resulting effective access must be clear.

**Result:** Not tested

**Findings / follow-up:**

<a id="settings-user"></a>

### User

**Page:** `/users/{name}`. Open a user from [Users](https://10.77.10.42/users); use only a disposable account for password/session, rename, disable and deletion experiments.

- [ ] Inspect the selected test account’s group, inherited/individual abilities, model limits, priority, sensitivity and tools.
- [ ] Sign in separately as that account; compare permitted pages/actions with the administrative view.
- [ ] On the test account, try rename, temporary password, API-key creation/revocation and disable/enable; inspect existing history attribution.
- [ ] Check that an operator cannot grant abilities beyond their own. Remove only the disposable account after finishing privacy checks.

**Result:** Not tested

**Findings / follow-up:**

<a id="settings-groups"></a>

### Groups

[Open Groups](https://10.77.10.42/groups).

- [ ] Inspect listed groups, memberships/limits and links to the selected group; check empty and restricted-access states.
- [ ] Create a disposable group only when permitted, check validation, and open its full detail page before assigning a test account.

**Result:** Not tested

**Findings / follow-up:**

<a id="settings-group"></a>

### Group

**Page:** `/groups/{name}`. Open a row from [Groups](https://10.77.10.42/groups); use a disposable group for policy and administrator-removal tests.

- [ ] Inspect the selected test group’s members, sign-in/request limits, abilities, allowed models and default tools.
- [ ] Assign your test account and check effective access in a separate sign-in/API client.
- [ ] Test a denied model/action and inspect the explanation; return the account to its original group before removing the disposable group.

**Result:** Not tested

**Findings / follow-up:**

<a id="settings-tools"></a>

### Tools

[Open Tools](https://10.77.10.42/tools).

- [ ] Inspect server descriptions, tool schemas/levels, tool sets and MCP addresses; check local and remote host distinctions.
- [ ] Connect using the tester and call a harmless read-only tool on scratch data; inspect inputs, result formatting and errors.
- [ ] With a restricted account/tool set/sensitivity, verify hidden tools cannot be called directly either.
- [ ] If changing a test tool set or level, save/reload and restore it; distinguish discovery failure from an executed call with no confirmed result.

**Result:** Not tested

**Findings / follow-up:**

<a id="settings-plugins"></a>

### Plugins

[Open Plugins](https://10.77.10.42/plugins).

- [ ] Inspect installed plugin folders, invalid/unsupported entries, enablement and authentication/readiness notices; follow the selected plugin link.
- [ ] Check that opening the catalogue does not imply its adapters are running or its public models are enabled.

**Result:** Not tested

**Findings / follow-up:**

<a id="settings-plugin"></a>

### Plugin

**Page:** `/plugins/{plugin}`. Open an installed entry from [Plugins](https://10.77.10.42/plugins).

- [ ] Inspect manifest/capabilities, plugin enablement and the instance list; compare the selected plugin with its catalogue entry.
- [ ] Open an existing instance and the Add instance form; check disabled/empty state and return navigation.
- [ ] **Optional maintenance:** disabling an in-use plugin can interrupt dependent requests; limit enablement changes to a disposable/idle target and verify Chat/API visibility afterward.

**Result:** Not tested

**Findings / follow-up:**

<a id="settings-new-plugin-instance"></a>

### New plugin instance

**Page:** `/plugins/{plugin}/instances/new`. Use Add instance on a selected [plugin](https://10.77.10.42/plugins); replace `{plugin}` with its installed ID.

- [ ] Open Add instance from a plugin; inspect instance ID, adapter address, sensitivity, settings and health/recovery defaults before saving.
- [ ] Check required/invalid fields without losing entered values; create only a deliberately selected disposable instance and inspect pending-versus-applied settings.
- [ ] Follow the saved instance to its detail page; verify model/profile aliases remain separately controlled. Record any created test ID and leave it disabled afterward; this page has no instance-delete control.

**Result:** Not tested

**Findings / follow-up:**

<a id="settings-plugin-instance"></a>

### Plugin instance

**Page:** `/plugins/{plugin}/instances/{instance}`. Open an instance from its plugin. Settings, health/recovery, public model aliases and the installer download all belong here; `/installer` returns a package rather than another page.

[Open Plugins](https://10.77.10.42/plugins).

- [ ] Open each installed plugin/instance; inspect configuration, adapter address, health, discovered profiles and public model aliases.
- [ ] Try Check health and discovery; compare enabled aliases with the Chat picker and API model list.
- [ ] Inspect settings synchronization and installer instructions without changing another profile's configuration.
- [ ] **Optional maintenance:** restart only a chosen idle adapter; check recovery, model availability and clear errors, then follow Plugin logs.

**Result:** Not tested

**Findings / follow-up:**

<a id="settings-voice"></a>

### Voice

[Open Voice](https://10.77.10.42/system/voice). This is Settings → Voice configuration; System → Voice has a separate diagnostics page.

- [ ] Inspect voice-service and gateway settings, direct/agent profiles, model choices, speaker choices, greetings and each setting’s application/restart requirement.
- [ ] Preview a voice or inspect existing greeting recordings; distinguish playback, saved configuration and regenerated audio. Follow System → Voice for call readiness and timings.
- [ ] For a suitable test profile, inspect validation/save/reload and restore its original values; do not change another profile.
- [ ] **Optional maintenance:** greeting regeneration and applying settings that require a service restart need a quiet window; retain existing recordings/configuration and verify the selected voice path afterward.

**Result:** Not tested

**Findings / follow-up:**

<a id="settings-audio"></a>

### Audio

[Open Audio](https://10.77.10.42/system/audio).

- [ ] Inspect global default models for Music, Sounds, Speech and transcription, runtime availability and any file-only/unavailable choices.
- [ ] Check field explanations and the Needs restart/Applies after openWeightGym restarts notices; personal choices belong separately under My account → Audio.
- [ ] If testing a reversible default change, preserve/restore the original value and avoid an unplanned service restart. Verify application later in an explicitly selected maintenance window.

**Result:** Not tested

**Findings / follow-up:**

<a id="settings-image"></a>

### Image

[Open Image](https://10.77.10.42/settings/image).

- [ ] Verify only administrators can change the global preview permission; saving applies immediately.
- [ ] Verify Civitai personal opt-in remains separate and defaults off; safe previews return when permission is disabled.
- [ ] Follow the links to image workflows and model/node placement.

**Result:** Not tested

**Findings / follow-up:**

<a id="settings-general"></a>

### General

[Open General](https://10.77.10.42/settings).

- [ ] Inspect program settings, descriptions, current values and restart/application status; distinguish front programs from per-node controls.
- [ ] Make one low-impact reversible change; save/reload, inspect validation and restore the original value.
- [ ] Inspect Model file locations for front and every configured node; check which machine each cache/storage root belongs to.
- [ ] Check secret/path presentation and unavailable-program feedback. Changing roots can make files appear missing, so keep path edits for a deliberate storage test.

**Result:** Not tested

**Findings / follow-up:**

## Docs

User guide is available to every signed-in account. System reference and its topic pages require administrator access. For each source page, inspect topic links, a representative Python/JavaScript module, signatures/line links and escaped source text; module query selections are views within the same page.

[User guide](#docs-user-guide) · [System reference](#docs-system-reference) · [API reference](#docs-api-reference) · [WeightRoom](#docs-weightroom) · [Turnstile](#docs-turnstile) · [LoadCoach](#docs-loadcoach) · [ToolLocker](#docs-toollocker) · [Voice](#docs-voice) · [Spotter](#docs-spotter) · [Hosted providers](#docs-hosted-providers) · [Plugins](#docs-plugins) · [Logs](#docs-logs) · [Models](#docs-models) · [Mirror Wall (images)](#docs-mirror-wall-images) · [Benchmarks](#docs-benchmarks) · [Data and backups](#docs-data-and-backups) · [Accounts and groups](#docs-accounts-and-groups) · [Collections and workflows](#docs-collections-and-workflows) · [Deployment and operations](#docs-deployment-and-operations)

<a id="docs-user-guide"></a>

### User guide

[Open User guide](https://10.77.10.42/guide).

- [ ] Open as an ordinary account and follow Chat, Collections, Workflows, Voice/media and API-key/address sections; terminology and available actions should match the console.
- [ ] Follow User guide from Home, Chat, Collections, Workflows, Voice and Account, then return without losing the selected page context.
- [ ] Compare the documented model API address with the installed configuration; distinguish trusted LAN/VPN HTTP, browser HTTPS and any administrator-configured gateway.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-system-reference"></a>

### System reference

[Open System reference](https://10.77.10.42/docs).

- [ ] [Open Docs Overview](https://10.77.10.42/docs); inspect the system map, ownership and links to component pages.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-api-reference"></a>

### API reference

[Open API reference](https://10.77.10.42/docs/api).

- [ ] Inspect OpenAI compatibility, unsupported APIs, custom endpoint methods/paths and accepted request headers; distinguish examples from supported operations.
- [ ] Follow a handler/source link and compare its route/fields with the referenced implementation. Examples must not expose a real key or imply permission bypass.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-weightroom"></a>

### WeightRoom

[Open WeightRoom](https://10.77.10.42/docs/weightroom).

- [ ] Inspect console, chat, administration and appearance references.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-turnstile"></a>

### Turnstile

[Open Turnstile](https://10.77.10.42/docs/turnstile).

- [ ] Inspect authentication, routing, placement and readiness/recovery references.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-loadcoach"></a>

### LoadCoach

[Open LoadCoach](https://10.77.10.42/docs/loadcoach).

- [ ] Inspect admission, residency, inference and request lifecycle references.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-toollocker"></a>

### ToolLocker

[Open ToolLocker](https://10.77.10.42/docs/toollocker).

- [ ] Inspect tool discovery, MCP, permissions and execution references.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-voice"></a>

### Voice

[Open Voice](https://10.77.10.42/docs/voice).

- [ ] Inspect gateway, clients, recognition, synthesis and administration references.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-spotter"></a>

### Spotter

[Open Spotter](https://10.77.10.42/docs/spotter).

- [ ] Inspect telemetry, service health, alerts and power-control references.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-hosted-providers"></a>

### Hosted providers

[Open Hosted providers](https://10.77.10.42/docs/hosted-providers).

- [ ] Inspect provider setup, credentials, routing, costs and limits.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-plugins"></a>

### Plugins

[Open Plugins](https://10.77.10.42/docs/plugins).

- [ ] Inspect adapters, installation, activity and error references.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-logs"></a>

### Logs

[Open Logs](https://10.77.10.42/docs/logs).

- [ ] Inspect request records, capture policy, tracing, diagnostics and reliability references.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-models"></a>

### Models

[Open Models](https://10.77.10.42/docs/models).

- [ ] Inspect files, presets, roles, projectors and embedding references.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-mirror-wall-images"></a>

### Mirror Wall (images)

[Open Mirror Wall (images)](https://10.77.10.42/docs/mirrorwall).

- [ ] Inspect image request flow, supported model families/parts, adding a model and reference-image guidance; follow links to the relevant source modules.
- [ ] Compare component, memory, capability and mask limitations with Image model settings and Image generation.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-benchmarks"></a>

### Benchmarks

[Open Benchmarks](https://10.77.10.42/docs/benchmarks).

- [ ] Inspect suites, scoring, qualification and run/schedule references.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-data-and-backups"></a>

### Data and backups

[Open Data and backups](https://10.77.10.42/docs/data).

- [ ] Inspect store ownership, backup/restore, retention and migration references.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-accounts-and-groups"></a>

### Accounts and groups

[Open Accounts and groups](https://10.77.10.42/docs/accounts).

- [ ] Inspect identity, sessions, permissions, sensitivity and portability references.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-collections-and-workflows"></a>

### Collections and workflows

[Open Collections and workflows](https://10.77.10.42/docs/collections).

- [ ] Inspect retrieval, prompt versions and workflow-state references.

**Result:** Not tested

**Findings / follow-up:**

<a id="docs-deployment-and-operations"></a>

### Deployment and operations

[Open Deployment and operations](https://10.77.10.42/docs/operations).

- [ ] Inspect topology, installation, configuration and recovery references.

**Result:** Not tested

**Findings / follow-up:**

## My account

[Account](#my-account-account) · [Appearance](#my-account-appearance) · [Audio](#my-account-audio) · [Logs](#my-account-logs)

<a id="my-account-account"></a>

### Account

[Open Account](https://10.77.10.42/account).

- [ ] Check name, group, sign-in expiry, session count and authenticator status.
- [ ] Sign out/in, inspect one incorrect-password response and the authenticator step if configured; avoid repeated failures that lock your account.
- [ ] Test password validation/change on the disposable account and check that other sign-ins end as described.
- [ ] Create a labelled test API key, use it once and revoke it; verify the revoked key is refused. A new key is shown only once.

**Result:** Not tested

**Findings / follow-up:**

<a id="my-account-appearance"></a>

### Appearance

[Open Appearance](https://10.77.10.42/account/appearance).

- [ ] Toggle top machine meters and confirm the saved choice after navigation/reload.
- [ ] Preview light/dark and available theme settings; try Save theme, Reset preview and Use default.
- [ ] Check text contrast, focus indicators, tables, charts and Chat under your preferred theme and window size.

**Result:** Not tested

**Findings / follow-up:**

<a id="my-account-audio"></a>

### Audio

[Open Audio](https://10.77.10.42/account/audio).

- [ ] Inspect personal download format, speech model, transcription model and voice; check defaults and unavailable/no-permitted-model explanations.
- [ ] Save a reversible preference, reload and check Music/Sounds/Speech selection or download behavior; restore the original choice afterward.
- [ ] Compare with a second account and Settings → Audio: personal choices should not silently change global defaults or another user’s settings.

**Result:** Not tested

**Findings / follow-up:**

<a id="my-account-logs"></a>

### Logs

[Open Logs](https://10.77.10.42/account/logs).

- [ ] Locate your recent sign-in/account/key actions; check timestamps, filtering and pagination where offered.
- [ ] Compare your ordinary account view with administrative logs and confirm the visibility makes sense.

**Result:** Not tested

**Findings / follow-up:**

## Sign-in and recovery

These pages/states are outside the signed-in navigation. They are included so entry, refusals and outages have their own findings records.

[Sign in](#sign-in-and-recovery-sign-in) · [Error and refusal pages](#sign-in-and-recovery-error-and-refusal-pages) · [Compute or machine unavailable](#sign-in-and-recovery-compute-or-machine-unavailable)

<a id="sign-in-and-recovery-sign-in"></a>

### Sign in

[Open Sign in](https://10.77.10.42/sign-in).

- [ ] Open a protected deep link while signed out; after a successful sign-in, confirm its safe same-site destination and query are retained, including through required password/authenticator steps.
- [ ] Inspect one incorrect-password response and focus/keyboard behavior without repeatedly failing a real account into lockout. Test credential changes only on a disposable account.
- [ ] If sign-in is already unavailable, check plain recovery guidance and preservation of the intended destination; do not stop the service merely to produce an outage.

**Result:** Not tested

**Findings / follow-up:**

<a id="sign-in-and-recovery-error-and-refusal-pages"></a>

### Error and refusal pages

[Open Error and refusal pages](https://10.77.10.42/no-such-page). The same error page family handles missing objects, HTTP refusals and server failures at their original URLs.

- [ ] Open an unknown URL and a nonexistent object: Page not found should name the missing resource appropriately, retain the console frame and offer Go to Home.
- [ ] Open a forbidden page with a restricted account: Access denied should name the needed ability, keep the reason visible and avoid highlighting an unrelated section or leaving an empty sidebar.
- [ ] If a server error occurs, inspect Something went wrong, its request ID and recovery link; private exception details must not appear. Do not force a production exception to tick this check.
- [ ] Check validation/refusal behavior on narrow screens and after waiting beyond temporary-notice duration; follow recovery links and browser Back.

**Result:** Not tested

**Findings / follow-up:**

<a id="sign-in-and-recovery-compute-or-machine-unavailable"></a>

### Compute or machine unavailable

Affected-page example: [Models](https://10.77.10.42/models). This is an alternate state of an affected page, not a new navigation destination. Use an already unavailable node or an isolated fixture; do not interrupt a host to create it.

- [ ] When a node is already unavailable, inspect the affected model/machine page, retained identity/history, availability message and disabled node-dependent actions.
- [ ] Check that front-owned Home, conversations, results and account pages remain readable; distinguish unavailable live readings from saved information.
- [ ] Inspect optional Wake guidance only when your account has that ability. Deliberate service outages or physical wake/suspend/reboot remain Optional maintenance, not a routine prerequisite.

**Result:** Not tested

**Findings / follow-up:**

## Routes that are not separate pages

| Route or view | Where to evaluate it |
|---|---|
| `/benchmarks` | Redirect to Runs, or Results for bookmarked result/model queries. |
| `/machines` | Telemetry alias. |
| `/cards` | Redirect to Compute nodes → Manage GPUs. |
| `/outside` | Cloud APIs compatibility redirect. |
| `/downloads`, `/strip`, `/pulse`, `?part=…` and SSE streams | Embedded progress/status/refresh behavior on their owning pages. |
| Collection originals, batch files, database/table dumps, plugin installers and the desktop ZIP | Download checks on Collection, Batches, DB, Plugin instance and Voice desktop app respectively. |
| `/voice/browser/config`, `/model-files`, `/mirrorwall-*`, `/soundroom-*`, `/loadcoach/*` and mutation-only actions | Supporting JSON/media/action endpoints; evaluate their visible outcomes through the owning page or connected-client checks. |

Tabs, model-type pills, node selectors, source-module selections, library dialogs and ordinary
validation responses remain inside their owning page checklist. Unused legacy templates do
not create additional pages. Migration preview is listed explicitly because its POST renders
a distinct review page; starting it is Optional maintenance.

## Connected clients

### Model API and MCP tools

- [ ] In an existing API client, list models through `http://10.77.10.42:8442/v1` using your test key; compare permitted names/order with the console.
- [ ] Try one ordinary reply, one streaming reply and cancellation; inspect model/role identity, response IDs and corresponding Requests evidence.
- [ ] If relevant, try an embedding or image request with a supporting model and an existing client.
- [ ] In an MCP client, connect to `http://10.77.10.42:8443/mcp` or a configured tool-set address; discover/call a harmless permitted tool and test a denied one.

Record the client name/version and whether failures are understandable from both the client
and openWeightGym. Keep the actual key in the client's private credential storage.

**Result:** Not tested

**Findings / follow-up:**

### Availability and recovery

- [ ] If a node is already unavailable, inspect which front-owned pages still work and whether dependent controls explain the missing node.
- [ ] Check whether retained conversations, results and account pages remain usable; distinguish saved information from current machine status.
- [ ] **Optional maintenance:** deliberately test a service outage/recovery only after active work drains; check recovery without duplicated requests or misleading success.

Physical sleep/wake and the planned clean reinstall can be evaluated separately using the
[installation guide](docs/installation.md). Do not stop services merely to tick this area.

**Result:** Not tested

**Findings / follow-up:**

## Wrap-up

### Restore the test environment

- [ ] Restore settings, formulas, permissions, model configurations and request-acceptance state changed during testing.
- [ ] Release your holds/locks; remove disposable schedules, keys, accounts/groups and test data you no longer want.
- [ ] Check that no test batch, benchmark, download or distribution is still running unintentionally.
- [ ] Keep useful issue/request IDs and identify every untested or blocked area before calling the walkthrough complete.

**Result:** Not tested

**Findings / follow-up:**

### Findings to act on

| Priority | Section / area | Finding and reproduction reference | Desired change / next step | Status |
|---|---|---|---|---|
| **P2 · U06** | System → Machine | Open Services, then click the second service's “Yes, act on …” label: the first service's checkbox changes. All three fixture controls have the literal duplicate ID `sure-{{ service.name }}`; subsequent checkboxes have no accessible name. No service action was submitted. [Reproduction](data/qualification/agent-walkthrough/20261031T125005-0700/machine-confirmation.json); [template](weightroom/pages/machine.html). | Build a unique ID from each service name and use it for the matching label; verify each label selects only its own checkbox. | Reproduced locally 2026-10-31; not fixed. |
| **P2 · O01** | Installation / local compute | Existing LoadCoach repeatedly fails startup with `FileExistsError`: `/home/jpk/ai/models/llm` is a dangling link to `/opt/models/llm`. Restart count increased between reads and TCP port 8442 refused connections. [Diagnosis](data/qualification/agent-walkthrough/20261031T125005-0700/loadcoach-diagnosis.json); [source pin](data/qualification/agent-walkthrough/20261031T125005-0700/loadcoach-source.json); [port check](data/qualification/agent-walkthrough/20261031T125005-0700/loadcoach-port.json). | Inspect the intended storage/mount target and repair it within a selected maintenance scope; verify real readiness afterward. | Observed read-only on this execution host 2026-10-31; unchanged. |
| | | | | |
| | | | | |
| | | | | |

**Agent recheck (2026-10-31):** V01–V04 and U01–U05 still reproduce against the current checkout; [fresh receipts](data/qualification/agent-walkthrough/20261031T125005-0700/finding-recheck.json). Remote active front/gateway sources could not be verified in this run. The [agent run](docs/agent-walkthrough.md#finish-and-report) records coverage and blockers; owner checklist results and prior acceptance are unchanged.

- **Most useful parts:**
- **Most confusing or frustrating parts:**
- **Missing features or information:**
- **Checks to repeat / still blocked:**
- **Next walkthrough session:**
