# SoundRoom audio integration plan

Status: initial integration deployed 2026-10-24; latest changes and verification are under **Execution status**. The original 21 production profiles have retained qualification evidence; additional runtime families are not automatically qualified. Spoken chat is switched on (`soundroom_profile = "soundroom"`, Chatterbox clone voice); [required service permissions](../voice/README.md#browser-call-settings) are restored, with personal HTTPS spoken-chat acceptance still pending. Owner interview held 2026-10-23; a staged full build with component installation, model downloads and local commits is authorized; never push. Where the build differs from a decision below, the row says so (A26, A40).

First release: music, sound effects, speech generation with voice cloning and design, transcription, and hands-free spoken chat. Final phase: opt-in audio.cpp speech for existing phone, desktop and browser voice.

## Confirmed requirements

- Generate music and sounds locally; also speech (text-to-speech with cloning and voice design) and transcription. Music is the main goal.
- Use [audio.cpp](https://github.com/0xShug0/audio.cpp) as the single native runtime. Keep the music contract engine-neutral so [acestep.cpp](https://github.com/ServeurpersoCom/acestep.cpp) can be added later without changing pages or the API.
- All new code lives in the `soundroom/` package, laid out like `mirrorwall/`.
- Chat gains a **Speech** page (Studio, Spoken chat and Transcribe tabs), a **Music** page and a **Sounds** page.
- Models gains an **Audio models** page: cache/storage file selection and per-node loading profiles; no pre-populated download menu (owner update 2026-11-12).
- Settings: administrator defaults under **System → Audio**; per-user preferences under **My account**.
- Optional audio.cpp installation during initial install, a standalone script to add it later, and manual directions. Model downloads use Suite's Files/Hugging Face machinery.
- LoadCoach admits and accounts for all audio work on GPUs and on explicit CPU profiles.
- OpenAI-compatible speech and transcription through Turnstile, plus a Suite job API for music and sounds.

## What audio.cpp provides and lacks

Reviewed 2026-10-23 from the upstream README, `app/server/README.md`, `app/server/api.md`, `docs/usage.md`, `docs/models/ace_step.md` and `docs/model_licenses.md`. Recheck these at the pinned revision; they are not Suite qualification.

| Consideration | Upstream state / implication for Suite |
|---|---|
| Code and weights | Code is Apache-2.0, v0.9.x. Weights keep their own licences, listed per family in `docs/model_licenses.md`. Several chosen families are non-commercial. |
| Server | `audiocpp_server` serves `/v1/audio/speech`, `/v1/audio/transcriptions` (+ `/details`, `/live`), `/v1/audio/alignments`, `/v1/audio/voices`, `/v1/tasks/run`, `/v1/tasks/stream`, `/v1/tasks/unload_models` and `/v1/tasks/unload_all_models`. It has no authentication: bind to loopback on a private port, as llama-server is. |
| Residency | One JSON config declares many models. `lazy_load`, `max_loaded_models` (LRU eviction of idle models), `idle_unload_ms` and `min_free_memory_mb` exist. LoadCoach must own eviction and keep upstream's own eviction disabled, using the unload endpoints. |
| Concurrency | One request per model at a time by default. `--parallel-jobs` with per-model `slots` (1–16) is marked experimental. `busy_timeout_ms` returns 503 `server_busy`; it does not cancel running inference. |
| Cancellation and progress | No cancel or job API. Progress exists only as log lines (`--log`). Stop therefore kills a process; percentage progress is only shown if the log format qualifies as stable. |
| Devices | One `--device` / per-model `device` index. Per-component placement across cards is not documented. Per-component weight types are (for example `ace_step.dit_weight_type`, `ace_step.planner_weight_type`). |
| Inputs | `request.audio` is a server-local path; `request.audio_base64` and `voice_ref` `{"type":"base64"}` take inline WAV (5 MiB decoded limit). Callers must never reach server-local paths. |
| Speech streaming | `/v1/audio/speech` with `stream_format` `sse` or `audio` streams PCM for families marked Stream. |
| Formats | GGUF packages at Hugging Face `audio-cpp/audio.cpp-gguf`; some families use safetensors. llama.cpp or whisper.cpp GGUFs are not interchangeable. |
| ACE-Step | Routes `text2music`, `complete` (continue), `cover`, `cover-nofsq`, `repaint` (with `--repaint-start/--repaint-end`), `lego`, `extract`. Options include `duration_sec`, `bpm`, `keyscale`, `timesignature`, steps, guidance, `sampler_mode`, seed. XL packages: bf16 14.2 GiB, q8 DiT 9.97 GiB. Quantizing the planner LM to q8 can fail; keep planner, text encoder and VAE at bf16. `ace_step.mem_saver` releases staged graphs between phases. |
| Build | CMake with `ENGINE_ENABLE_CUDA`, `GPU_TARGETS`, `AUDIOCPP_MODEL_SET` (`full`, `core`, `custom`), `AUDIOCPP_BUILD_SERVER_FRONTENDS`. Upstream ships no Linux CUDA binary, so Suite builds from source. |

## Integration contracts

### Code layout

`soundroom/` follows `mirrorwall/`: `engine.py` (LoadCoach engine adapter), `install.py` (pinned optional runtime), `model.py` (audio model settings and resource profiles), `requests.py` (strict request normalization), `front.py` (front API, assets and existing-batch integration), `store.py` and `schema.py` (front-owned private assets), plus `catalog.toml`, `pages/` and `static/`. Register the engine in `engines/__init__.py`. Add files only when a stage needs them.

Where SoundRoom needs code Mirror Wall already has (private asset store, SSE progress helpers, batch binding), move the shared part to one place used by both instead of copying it. Do that during the merge if Mirror Wall is still changing.

### Engine and admission

Engine id `audiocpp`. **As built**, every audio profile is one LoadCoach child (`soundroom/runtime.py`) with a private token on a loopback port, which starts one `audiocpp_server` for that one model with the WebUI off, upstream eviction off and `TMPDIR` in its own folder (the server unpacks package configs there and never removes them). It stays idle-resident until its memory is needed, like other Suite models. A request that does not finish, including Stop, kills the native server with SIGKILL (it crashes on SIGTERM mid-run); the reservation is released only after the process exits. The next request reloads.

Music and sound profiles hold their card alone (ACE-Step turbo bf16 peaked at 15.6 GB of 16 GB). Speech and transcription profiles may share a card once LoadCoach has measured them, as llama.cpp models do. This is not the shared speech server per card of decision A26: one process per profile fits LoadCoach's one-model-per-process admission and accounting as it is, and Stop on music never touches speech, which was the decision's purpose. A shared server is an optimization to revisit if many small speech models compete for one card.

Each profile names its device (`cuda0`, `cuda1` or `cpu`), the per-component weight types, session options such as `mem_saver`, allowed durations and input sizes, and host-RAM budget. CPU profiles are explicit; a GPU profile never falls back to CPU silently. A two-card component placement is offered only for a family where the pinned build demonstrably places components on separate devices. Otherwise the profile runs on one card if it fits, or is marked not runnable, with the reason shown.

Priority classes, highest first: **live** (spoken-chat recognition and synthesis), **short** (Studio lines, transcription, test actions), **batch** (music, sounds, long scripts). Batch work yields at job and take boundaries, never mid-native-call. Live sessions take a hold like current voice sessions. The shared speech server's per-model concurrency (default one, optional experimental `slots`) is a measured limit on how many live sessions a model serves.

Profile identity includes the family, package revision and file hashes, weight types, device layout and duration bounds. Measured footprints (load and steady peak per card, host RSS, real-time factor) are recorded per profile from test actions and real runs, and admission uses them once recorded.

Installed VoxCPM2 clone profile, 2026-10-25: session option
`voxcpm2.audiovae_encoder_sample_capacity = 960000` reserves 40 seconds at 24 kHz;
the native default of 240000 samples rejected saved 28–33 second reference clips.
Longer references need a larger configured capacity and new resource qualification. The original
profile is retained on compute in `data/installation/speech-repeat-20261025/voxcpm2-clone-before.json`;
restore its JSON through the existing model-settings API to undo this setting.

### acestep.cpp, reserved

Engine id `acestep_cpp` is reserved; no code until the owner asks. Music requests, presets and pages refer to a music task contract (mode, caption, lyrics, duration, tempo/key/metre, seed, steps, guidance, source audio and repaint window) rather than audio.cpp field names. A future adapter maps that contract to acestep.cpp's own server and is selected by the profile's engine, like `sdcpp` and `llama_cpp`.

### Optional installation and maintenance

Same contract as the Mirror Wall runtime installer:

- **Initial installation:** when LoadCoach is selected, ask "Install optional audio.cpp audio support?" before the final summary, default No on a fresh install, offer to keep an existing compatible installation. Front-only installations explain that the runtime belongs on the compute host.
- **Unattended:** `--audiocpp install|skip|keep`. Omission keeps an existing installation or skips a fresh one; `--yes` alone never enables it. Show it in `--dry-run`; `--check` audits without building or loading a model.
- **Install later:** idempotent `scripts/install-audiocpp.sh` calling `soundroom/install.py`, also called by the main installer. It never resets settings, keys, databases or model files.
- **Build:** pinned upstream revision; CUDA build with `GPU_TARGETS` for the detected cards (sm_120 for the RTX 5060 Ti), `AUDIOCPP_MODEL_SET=full`, server frontends off. Always a bounded job count (`-j 6`); an unbounded `-j` once exhausted host memory. Reuse the CUDA 13.1/glibc 2.43 include overlay at `~/ai/tools/cuda-13.1-include-overlay` if the same clash appears. Build in a staging directory, verify the executable and its family list, activate atomically, keep the previous build for rollback. No CPU substitution for a failed CUDA build.
- **Failure:** report what succeeded and failed, keep any previous working build, allow a retry. Without audio.cpp, audio pages and routes show "audio.cpp not installed" with setup directions, and the rest of Suite is unaffected.
- **Manual installation:** a section in [installation.md](installation.md): prerequisites, pinned source, exact CMake commands, placement and registration, verification, a short opt-in smoke run, update/rollback and troubleshooting.

### Models, files and catalogue

Add an `audio` folder to the cache and storage roots beside `llm`, `adapters` and `projectors`. Extend Files/Hugging Face downloads beyond `.gguf` so a package directory (GGUF, safetensors, JSON and tokenizer files) installs as one verified bundle, atomically, with hashes and source revisions recorded. Shared files are tracked once and protected while any profile uses them. `audiocpp_model_manager` is not used at runtime. Coordinate with Mirror Wall's equivalent non-GGUF extension at merge.

The Audio models form starts with actual cache/storage files and optional companion files. Obtain other weights through Files/Hugging Face; there is no pre-populated download menu. Runtime families and declared load/session/request options come from the pinned release's `model_specs/`, with a selected audio.cpp GGUF's embedded contract as a fallback. The installed runtime must implement the family's architecture: an arbitrary GGUF is not automatically compatible. `soundroom/catalog.toml` supplies legacy task hints, licence metadata and family-specific caption/lyrics mappings; it is not a family allowlist. Tasks without Suite routes remain unavailable. Native option names must be declared by the contract. The existing package installer remains available explicitly through the CLI/API. The Audio models page shows installed profiles and per-node configuration.

Default precision per family: the highest precision whose measured peak fits its card and passes the metric checks; q8 only where bf16/f16 does not fit.

Original integration families (historical coverage, not a model-selection limit):

| Task | Families |
|---|---|
| Music | ACE-Step 1.5 (`ace_step`), Stable Audio 3 (`stable_audio`), HeartMuLa 3B (`heartmula`), YuE2 3B (`yue2`) |
| Sounds | Stable Audio 3 Small SFX (`stable_audio`), MiDashengLM-Gen (`midashenglm_gen`) |
| Speech, cloning, design | Qwen3-TTS (`qwen3_tts`), Chatterbox and Turbo (`chatterbox`, `chatterbox_turbo`), VibeVoice (`vibevoice`), VoxCPM2 (`voxcpm2`), IndexTTS2 (`index_tts2`) |
| Transcription | Parakeet TDT (`parakeet_tdt`), Qwen3-ASR with forced aligner (`qwen3_asr`, `qwen3_forced_aligner`), Canary (`canary_asr`), Nemotron (`nemotron_asr`), diarization (`sortformer_diar`, `nemotron_3_diar`) |

### API

Through Turnstile with existing keys, model permissions and signed routing:

- `POST /v1/audio/speech` and `POST /v1/audio/transcriptions`, OpenAI-shaped, including multipart upload. Verify Turnstile forwards multipart bodies intact.
- A Suite job API for music and sounds, built on the existing front batch executor as Mirror Wall's front is: submit returns a job id; an SSE stream reports state and finished takes; the result can be fetched; cancel stops the job. No new scheduler.
- Audio models publish explicit capabilities (`speech`, `clone`, `design`, `transcription`, `music`, `sound`) so they never satisfy chat roles or routes.
- `requests.py` accepts only inline audio or Suite asset ids, never server-local paths, `voice_dir` names or native arguments. Unsupported settings are refused with a reason, not ignored.

### Music page (Chat → Music)

- Mode per family: song, instrumental, continue (`complete`), cover from reference audio, repaint a time range.
- Fields and limits come from the selected profile (upstream `GET /v1/models?include_params=true` plus Suite limits): style caption, lyrics, duration up to the model maximum, tempo, key, metre, seed, steps, guidance and sampler where supported. Less-used fields go under Advanced.
- Lyrics editor with section-tag help. **Write with a model** turns a short idea into a style caption and tagged lyrics, using a chat model chosen in settings, through Turnstile with the caller's own key.
- Generate makes 1–N takes of one prompt as one queued batch job; takes appear as each finishes.
- Track list of takes (title, model, duration, state) and a waveform player for the selected take: play, pause, seek, loop region, download, **Reuse settings**, and **Edit this range** (opens repaint with the selected region).
- Reference uploads accept WAV, FLAC, MP3 and Opus, decoded and resampled on the server as the family requires; originals are kept.

### Sounds page (Chat → Sounds)

Prompt, duration and seed for the two sound families; N variations per submit; an optional seamless-loop output with a crossfaded seam; a saved library with tags, search and favourites. Same player and downloads as Music.

### Speech page (Chat → Speech)

**Studio tab**: text or a multi-speaker script (`Speaker 1:` lines mapped to voices). Long text is split at sentences, synthesized in order and joined into one file as a batch job, with progress per chunk. Voice choice: family presets, saved voices or a new reference.

**Voice library**: upload a clip or record in the browser (HTTPS or localhost); the reference transcript is filled by transcription and editable; voice design generates a voice from a description and saves it. Saving a cloned voice requires ticking "I have the speaker's permission", recorded in the front event log. Voices are private and their owner can share them with groups. A voice is stored as a reference clip plus transcript, which every cloning family accepts; any family-specific cached embedding is a derived file keyed by family and revision.

**Transcribe tab**: upload or record; choose a model; optional diarization and word timing; results listed with their audio; copy and download as txt, srt, vtt or json.

**Spoken chat tab**: hands-free conversation with a chosen chat model in a chosen voice, through the existing Voice gateway and sound socket in `direct` mode, which keep their capture, bounded buffers, barge-in, holds and failure handling ([voice.md](voice.md#reliability-and-lifecycle)). Browsers allow the microphone only over HTTPS or on localhost, so the tab opens the private HTTPS voice page with the chosen voice. **As built**: a voice profile with `speech = "audiocpp"` (`soundroom/voice.py`) gathers sound until a pause (energy endpointing, default 700 ms) and transcribes the utterance whole on a transcription model, then speaks each phrase through `/v1/audio/speech` in the session's voice, all through turnstile with the service's key and an optional `priority = "high"`. The gateway offers the browser account's SoundRoom voices when `soundroom_profile` is set, fetches the chosen voice's reference with the caller's key, and uses that profile for the call only; other calls are unchanged. Each finished exchange is kept as a WeightRoom conversation; **Record the call**, off by default, keeps both sides as Opus SoundRoom recordings at hang-up.

### Settings

- **System → Audio**: default profile per task, per-task duration and size limits, priority-class settings, live-session limits, the lyrics helper's chat model, retention of intermediates. Add every key to the example TOML and the live TOML in the same change.
- **My account**: default voice, default download format, default speech and transcription models.

### Storage, sharing and downloads

Front owns private per-user assets (references, voices, takes, sounds, transcripts, recordings), retained until the owner deletes them, with download and delete controls. Owners can share a voice, take or sound with groups. No-log work uses temporary assets with cleanup. Workers own model files and scratch.

Downloads offer WAV, FLAC, MP3 and Opus, encoded with the host's `ffmpeg` (`flac`, `libmp3lame`, `libopus` present). Prompt, lyrics and settings are never embedded in downloaded files. Downloading a take or sound made by a non-commercial model shows its licence note once per session.

### Qualification

Acceptance is by metrics only; no owner listening gate. These prove function and format, not musical quality:

- Every output: requested duration within tolerance, sample rate and channels as declared, no NaN, no silent output, clipping below a bound, loudness recorded.
- Fixed-seed determinism per profile where the family is deterministic.
- Speech: transcription round-trip word error rate per voice and family; speaker similarity for cloned voices (cosine of WeSpeaker ResNet34 embeddings through sherpa-onnx, against the reference and against a different voice).
- Transcription: word error rate on a fixed local set; diarization on a fixed two-speaker set.
- Loops: seam continuity.
- Resources: per-card and host peaks within the profile; cleanup after Stop, crash and OOM; batch work yields to live sessions; LLM and image work keep their admission.

## acestep.cpp later, and voice migration

Final phase: the voice service's audio.cpp speech implementation becomes selectable per session for phone, desktop and browser voice. sherpa-onnx stays the default. acestep.cpp stays a reserved engine id until the owner asks.

## Interview decisions

Owner answers from 2026-10-23. Open rows are recommendations; ask before relying on them.

| ID | Decision | Owner answer / proposed choice | State |
|---|---|---|---|
| A1 | Runtime | audio.cpp only; acestep.cpp reserved as a plan section and engine id, no code. | Confirmed |
| A2 | Existing voice service | Separate now; final phase lets voice sessions opt in to audio.cpp, sherpa-onnx stays default. | Confirmed |
| A3 | Devices | GPUs through LoadCoach plus explicit CPU profiles. | Confirmed |
| A4 | First-release scope | Music, sounds, speech with cloning and design, transcription. Stem separation excluded. | Confirmed |
| A5 | Music families | ACE-Step 1.5, Stable Audio 3, HeartMuLa 3B, YuE2 3B. | Confirmed |
| A6 | Non-commercial licences | Allowed. Licence and commercial flag on Models; NC outputs confirm on download, once per session. | Confirmed |
| A7 | Music features | Text and lyrics to song, edit/repaint/continue, reference audio and cover. | Confirmed |
| A8 | Sounds | Separate Sounds page; Stable Audio 3 Small SFX and MiDashengLM-Gen; seamless loop, variations, tagged library. No video foley. | Confirmed |
| A9 | Lyrics helper | A chat model drafts lyrics and the style caption from an idea. | Confirmed |
| A10 | Track length | Up to each model's maximum. | Confirmed |
| A11 | Music browser | Track list with waveform player, loop region, reuse settings, edit range. | Confirmed |
| A12 | Formats | WAV, FLAC, MP3, Opus. | Confirmed |
| A13 | Speech page | Tabs: Studio, Spoken chat, Transcribe. | Confirmed |
| A14 | Voice capture | Upload, browser recording, saved library, voice design. | Confirmed |
| A15 | Voice sharing | Private, owner can share with groups. | Confirmed |
| A16 | Cloning safeguard | Permission checkbox, recorded in the event log. | Confirmed |
| A17 | Speech families | Qwen3-TTS, Chatterbox and Turbo, VibeVoice, VoxCPM2, IndexTTS2. | Confirmed |
| A18 | Transcription families | Parakeet TDT, Qwen3-ASR, Canary, Nemotron, diarization. | Confirmed |
| A19 | Long text | Multi-speaker script mode with split and merge. | Confirmed |
| A20 | Spoken chat | Hands-free with barge-in, through the existing Voice gateway in direct mode. | Confirmed |
| A21 | Spoken chat history | Text saved in Conversations; audio recording opt-in, off by default. | Confirmed |
| A22 | Model downloads | Suite Files/Hugging Face machinery. | Confirmed |
| A23 | Audio models page | Choose existing cache/storage files and companion files, profiles and per-node configuration; no pre-populated downloads. | Updated by owner 2026-11-12 |
| A24 | Runtime build | Source, pinned, CUDA, full model set. | Confirmed |
| A25 | Settings | Administrator defaults in System → Audio; personal preferences in My account. | Confirmed |
| A26 | Processes | Shared speech server per card; one generation server per music/sound/long-speech profile. | Confirmed; **built as one server per profile** (see Engine and admission) |
| A27 | Stop | Kill the generation process; release after cleanup is confirmed. | Confirmed |
| A28 | Priority | Live, then short, then batch. | Confirmed; batch jobs run `low`, live speech `high` when its profile sets `priority` |
| A29 | API | OpenAI speech and transcription; Suite job API for music and sounds. | Confirmed |
| A30 | Storage | Like images (private until deleted, no-log temporary), plus sharing with groups. | Confirmed |
| A31 | Two cards | Component placement where the build supports it; otherwise one card, refused clearly. | Confirmed |
| A32 | Build order | Runtime and models, then Music, Sounds, Speech, Spoken chat. | Confirmed |
| A33 | Quality acceptance | Metrics only. | Confirmed |
| A34 | Download metadata | Never embed prompt, lyrics or settings. | Confirmed |
| A35 | Music batch | 1–N takes per submit. | Confirmed |
| A36 | Precision | Highest precision that fits and passes. | Confirmed |
| A37 | Location | `soundroom/` package; this branch merges after the Mirror Wall session. | Confirmed |
| A38 | Authorization | Full staged build with downloads and local commits; no push. | Confirmed |
| A39 | Page name | The existing Chat → Voice link opens the browser voice app, so the new page is **Speech**. | Built as recommended; owner may rename |
| A40 | Live recognizer | Pick by metrics among streaming families (`nemotron_asr` streaming, `vibevoice_asr_streaming`, `voxtral_realtime`). | Open; built with whole-utterance transcription after a pause (no partials), which any transcription profile serves |
| A41 | Speaker similarity | WeSpeaker ResNet34 (`wespeaker_en_voxceleb_resnet34_LM.onnx`, sherpa-onnx, already a voice-service dependency) in the qualification harness only. A clone passes when it is nearer its reference than a different voice. | Confirmed |

## Delivery order and acceptance

1. **Runtime**: `soundroom/install.py`, `scripts/install-audiocpp.sh`, installer flag and prompt, manual directions. Verify accept, decline, unattended choices, front-only, dry-run, check, existing builds, failed builds, retry and rollback.
2. **Engine and models**: `audiocpp` adapter, speech and generation servers, profiles, admission, cleanup; the `audio` root, bundle downloads, `catalog.toml`; the Audio models page with file-based setup and profiles. Download and test ACE-Step first, then the other music families.
3. **Music**: job API, front assets, Music page, lyrics helper, takes, player, repaint/continue/cover, downloads and the NC note. Qualify each music family by metrics.
4. **Sounds**: families, variations, loops, tagged library.
5. **Speech**: speech and transcription routes, Studio, voice library with consent and sharing, script mode, Transcribe tab, speech and transcription families.
6. **Spoken chat**: audio.cpp speech implementation in the voice service, live class and holds, Spoken chat tab, history and recording toggle. Verify barge-in, overload, crash and key-revocation behaviour from voice.md.
7. **Settings and operations**: System → Audio, My account preferences, TOML files, Logs/Status/Files integration, maintained docs.
8. **Coexistence**: LLM, image and audio queues together; peaks, holds, Stop at every stage, OOM and crash cleanup, restart behaviour, permissions, sharing and no-log cleanup.
9. **Merge and deploy**: after the Mirror Wall session finishes, merge `soundroom` into `main`, resolve shared files, rerun the full suite, deploy after work drains.
10. **Final phase**: opt-in audio.cpp speech for existing voice sessions.

## Execution status

- File-based audio models, 2026-10-26: the pre-populated download menu and family allowlist are removed; runtime/embedded contracts govern compatibility. The [deployment](operations.md#runtime-source-overrides) includes live file/family selection and a fresh Kokoro speech check. Kokoro 82M BF16 is installed and hash-verified in compute cache, registered as `kokoro` on CPU; its initial `/v1/audio/speech` check returned a 3.875 s, mono 24 kHz WAV in 7.389 s including load ([verification](../data/installation/kokoro-20261026/verification.json), [sample](../data/installation/kokoro-20261026/speech.wav)). These are synthesis smoke checks, not full voice-quality qualification.
- Repeated clone fix, 2026-10-25: [release and recovery](installation.md) deployed across three hosts; 106 relevant frozen-source checks passed. Live Speech completed three consecutive Chatterbox, Qwen3-TTS and VoxCPM2 clones each (nine successful jobs, including a 33 s reference); [incomplete catalogues](loadcoach.md#answering-a-batch) now wait, and the VoxCPM2 capacity setting above removes the separate long-reference failure. These checks establish completion; voice similarity remains separately qualified.
- 2026-10-23: plan written on branch `soundroom` (worktree `~/ai/worktrees/soundroom`, from `f29ff5df`). No code yet.
- Stage 1 (runtime): `soundroom/install.py`, `scripts/install-audiocpp.sh`, `--audiocpp install|skip|keep` in the Suite installer and the manual section in installation.md. Pinned v0.9.2 built here with CUDA (`120a-real`, 6 jobs), adopted through the installer into `~/.local/share/suite/audiocpp/releases/0097d365…-cuda`; `--check` verifies hashes and flags; `--list-devices` shows both RTX 5060 Ti cards. Registered only in the worktree's private `settings/loadcoach.toml`: the main checkout's LoadCoach does not know engine `audiocpp` until the merge. The native server enables its WebUI by default, so launches pass `--no-ui`.
- Stage 2–5 (engine, packages, Music, Sounds, Speech, settings): built on the branch and merged with Mirror Wall's front design. GPU qualification with `scripts/qualify-soundroom.py` (this checkout's LoadCoach, Turnstile and WeightRoom in one delegated transient service, under the installed LoadCoach's lock): [run c](../data/qualification/soundroom-20261024-c/report.json) passed every step. ACE-Step turbo bf16: two 20 s takes in about 30 s with load, peak 15.6 GB on its card; repaint keeps the source length; Stop frees the card in under a second. Stable Audio music and SFX (seamless loop), HeartMuLa (its lyrics transcribed back), MiDashengLM music and SFX, Qwen3-TTS cloned script and designed voice (both transcribed back word for word), Parakeet on the CPU, Sortformer speaker turns (two speakers, correct labels), and the voice-service speech path (utterance heard in 0.22 s; a 2 s reply synthesized in 6 s). All pages load without script errors and without sideways scrolling at phone width.
- Found and fixed on the GPU: model saves refused `audiocpp` settings; per-process VRAM readings missed most of audio.cpp's use (now the card is sampled); an idle engine holding a host-RAM budget blocked CPU models until they timed out (`Runner.cpu_room` now evicts it, with a regression test); the native server crashes on SIGTERM mid-run (Stop uses SIGKILL); it refuses a folder with two GGUFs (a profile's path is its main GGUF); speaker labels arrive as `speaker_id` with sample offsets; a 4 GB host-RAM budget is too small for long ACE-Step takes (music and sound default to 8 GB).
- Known limits (at run c; see the run d entry for what changed): YuE2 has no duration option and generated 6 minutes for a 20 s request, so it has no production profile yet; synthesis for spoken chat is slower than speech (6 s for 2 s); cloned-voice similarity is not measured (A41); recording in the browser needs HTTPS, so on the LAN console use upload.
- Stage 6 (spoken chat): `soundroom/voice.py`, per-profile speech in the voice service, voice choice, conversation keeping and optional recording in the gateway, tests for each. Not deployed: switch it on by setting a `[profiles.soundroom]` voice profile on compute (see settings/examples/voice.toml), `soundroom_profile` on the gateway, moving `voice.service` and `voice-gateway.service` to the release, and giving the voice account the audio models.
- Deployment 2026-10-24: `main` fast-forwarded to `3d41f8d5` (12 commits ahead of origin, not pushed). Immutable export at `~/.local/share/ai-suite/releases/soundroom-3d41f8d5/source` on both machines. Compute: `[engines.audiocpp]` added to `settings/loadcoach.toml` by the installer (preimage in `data/installation/audiocpp-backups/` and `data/installation/soundroom-deploy-20261024/`), drop-in `loadcoach.service.d/80-soundroom-release.conf`, LoadCoach restarted while idle. Front: `DEPLOY_SOURCE=… scripts/deploy-front.sh` copied the code and new defaults; WeightRoom failed to start for about a minute because its units still ran the Mirror Wall release against the new defaults, then drop-ins `70-soundroom-release.conf` for weightroom and turnstile fixed it and both restarted (settings copy in `~/weightroom-settings-before-soundroom-20261024`). Ten production audio profiles added (ace-step, stable-audio-music, heartmula, midasheng-music, stable-audio-sfx, midasheng-sfx, qwen3-tts-clone, qwen3-tts-design, parakeet, sortformer); a 10 s ACE-Step take through the public API took 22 s cold and was kept on front.
- Rollback: remove the two front drop-ins and the compute drop-in, restore the compute `settings/loadcoach.toml` preimage (the earlier release does not know engine `audiocpp`), `systemctl --user daemon-reload`, and restart the same services after work drains. SoundRoom's front tables and `data/soundroom-assets/` can stay.
- Qualification [run d](../data/qualification/soundroom-20261024-d/report.json) (with `--skip music`) and [run e](../data/qualification/soundroom-20261024-e/report.json) (VibeVoice after its fix), on CUDA1. YuE2's length is now its semantic frame count (25 per second, both token bounds), so a 20 s request gave 19.999 s in 13 s with its lyrics heard back. Every speech family spoke a 19-word passage that Parakeet heard back with no word errors; warm synthesis runs at 0.3–0.7 of real time, and a short live reply (`/v1/audio/speech`, PCM) takes about 1 s for 2 s of speech once the model is resident: the 6 s at run c was a cold load. Cloned-voice similarity to a designed reference (A41) against a different voice: Chatterbox 0.825/0.533, IndexTTS2 0.820/0.635, VoxCPM2 0.788/0.645, Qwen3-TTS 0.6B 0.786/0.573, Qwen3-TTS 1.7B 0.764/0.552. Transcription of a natural and an espeak recording: Parakeet 0/0.11 word error rate in 0.3 s on the CPU, Qwen3-ASR 0/0.11 on the GPU, Canary f32 0/0.44, Nemotron ASR 0.17/0.44. Nemotron 3 diarization (commercial use allowed, unlike Sortformer) labelled the two-speaker dialogue correctly.
- Found and fixed at run d: VibeVoice reads only `Speaker N:` lines (catalog `speaker_label`); a job whose every take failed was shown as completed with no reason (front state now says failed and why); the harness's `--skip browser` was ignored.
- Deployment b, 2026-10-24: release `soundroom-522654d8` exported to both machines. Compute: drop-ins `loadcoach.service.d/80-soundroom-release.conf` (moved) and `voice.service.d/80-soundroom-release.conf` (new); `[profiles.soundroom]` added to `settings/voice.toml` (parakeet, chatterbox-clone, priority high); LoadCoach and voice restarted while idle; eleven more production profiles (yue2, chatterbox-clone, chatterbox-turbo, voxcpm2-clone, voxcpm2-design, index-tts2, vibevoice, qwen3-asr, canary, nemotron-asr, nemotron-diar). Preimages in `data/installation/soundroom-deploy-20261024-b/`. Front: drop-ins `70-soundroom-release.conf` for weightroom, turnstile and voice-gateway (new), `soundroom_profile = "soundroom"` in `settings/voice-gateway.toml` (preimages in `~/soundroom-deploy-20261024-b/`), all three restarted. A live 20 s YuE2 take took 7.6 s and measured 19.999 s.
- Found at deployment b: a site edit of the voice service's `profiles` replaced the whole table, hiding the file's new profile; profiles now merge like optional engines (`loadcoach/settings.py`). `weightroom user adjust NAME add=…` replaces the account's own additions rather than adding to them, so giving voice-service `high` took away its `hold`, `load` and `see-all`; required runtime permissions are now restored (see [call settings](../voice/README.md#browser-call-settings)).
- To use spoken chat: share a voice with the voice page's account group (`voice-browser`) under Speech → Voices, then choose it on the HTTPS voice page.
- Usability follow-up, 2026-10-24: [Audio workflow](README.md#audio) documents corrected reuse, paginated/tag-filtered libraries, default voices, failure/retry controls and model-aware music direction; Collections/Workflows now follow Conversations. Validation: 103 focused checks passed (seven Chromium interactions), then 53 relevant checks reran after visual polish; authenticated live Music reuse/variation, Sounds and Speech controls, scripts, 21 model profiles and library contracts verified without browser errors ([Music screenshot](../data/console-previews/soundroom-live-music.png)). This does not establish an audible quality improvement. Current source, combined validation and recovery are recorded in the [combined deployment](image-generation-plan.md#combined-deployment-2026-10-24).
- Completion check, 2026-10-24 (on the combined release `unified-395fb7ac`, which every compute and front service runs; `main` differs from it only in documents): delivery stages 1–5, 7 and 9 are done and qualified; stage 10 (audio.cpp speech for voice sessions) is built and switched on. Stage 8, coexistence, passed live through the public API ([timeline](../data/qualification/soundroom-20261024-coexistence.json)): a 30 s ACE-Step take held CUDA0 alone while a chat answer came from `qwen-2.5-1.5b` on CUDA1 at 8 s and Parakeet transcribed on the CPU at 10 s; a klein image waited for CUDA0 and ran once the take ended (ACE-Step was evicted for it), all four answering 200. Remaining: the owner's spoken-chat call in a shared voice, which also exercises stage 6's live barge-in. acestep.cpp stays reserved by decision A3.

