# Voice service brief

The voice gateway handles browser, desktop and Twilio connections on the control node.
Compute runs speech recognition, speech synthesis and local inference. Capacity and model
readiness depend on your configured profiles; inspect **Voice status** before testing.
Telephone setup, credentials, numbers and caller access are managed in **System → Voice →
Telephone**. Follow the [telephone guide](../docs/telephone.md) for public HTTPS routes,
SMS registration and troubleshooting. Existing machine-file Twilio settings keep working
until explicitly imported in the panel.

Historical deployment qualifications remain in the [voice acceptance ledger](../docs/voice.md#voice-latency-diagnostics-c06-milestone-100).
The addresses below are placeholders for your installation.

```mermaid
flowchart TD
    Desktop["Desktop app<br/>Linux / Windows / macOS"]
    Browser["Phone or desktop browser"]
    Twilio["Twilio telephone call"]

    Desktop -->|"LAN · API key · port 8447"| Gateway
    Browser -->|"HTTPS · Tailscale Serve :443"| Serve["control.your-tailnet.ts.net"]
    Serve -->|"localhost :8446"| Gateway
    Twilio -->|"HTTPS / WSS"| VPS["voice.example.com<br/>VPS Caddy :443"]
    VPS -->|"Public-only listener · :8081"| Gateway

    subgraph ControlNode["Control node · control-node.lan"]
        Gateway["Voice gateway"]
        Turnstile["Turnstile<br/>Authentication / readiness / preparation"]
        Hermes["Optional Hermes agent"]
        Gateway --> Turnstile
        Gateway <--> Hermes
    end

    subgraph Compute["Compute PC · compute-node.lan"]
        Voice["Voice service :8445<br/>Recognition + synthesis"]
        Model["LoadCoach :8442<br/>Local language model"]
    end

    Gateway <-->|"Streaming audio"| Voice
    Turnstile -->|"Session preparation · :8445"| Voice
    Turnstile -->|"Authentication / model requests · :8442"| Model
    Voice --> Model
    Hermes -->|"Model requests"| Turnstile
```

The conversation supports two modes. **Direct** uses the local model for replies.
**Hermes** adds an agent and tools while retaining the same speech pipeline.

```mermaid
flowchart LR
    Audio["Incoming audio"] --> ASR["Speech recognition"]
    ASR --> Mode{"Mode"}
    Mode -->|Direct| Model["Local model"]
    Mode -->|Hermes| Agent["Hermes + tools"]
    Agent <--> Model
    Model -->|Direct reply| TTS["Speech synthesis"]
    Agent -->|Final reply| TTS
    TTS --> Playback["Audio playback"]
```

## Operator pages

Milestone 89 is deployed; [H2 owner acceptance](../docs/voice.md#h2-owner-acceptance--2026-10-16) closes its page/playback gate.
**Chat → Voice** opens the browser conversation at `/voice`; **Voice desktop app** opens
the ZIP and platform setup instructions at `/voice/desktop`.
**System → Voice** (`/system/voice`, `manage-settings`) provides server/readiness status,
Direct/Hermes/shared settings, voice previews, greeting text editing and regeneration, and
playback of the actual control node cache. Existing Settings forms edit the same values. Status
and cached playback do not wake compute; explicit previews use CPU synthesis. Edited
phrases and speaker selection regenerate a complete versioned set, retained on the control node for
compute outages. Public Twilio and desktop ports cannot reach the private admin controls.
See [installation](../docs/operations.md#voice-pages-and-home-summaries) for delivery.

### Browser call settings

The browser page uses **Transcript studio**: the live transcript beside a model setup rail,
with call controls and language choices below. Choose the **Conversation model (LLM)** and
**Speech engine** before starting. **CPU voices** uses the installed recognition/synthesis
bundle and its built-in voice list. **Audio models** enables independent **Recognition (STT)**
and **Speech (TTS)** models. Speech models offer their declared built-in voices; cloning models
use your saved or shared SoundRoom voices. A model without a declared voice list offers its default.

Choose **I speak / listen in** for recognition. Enable **Translate replies** to select a different
response language; otherwise replies match the listening language. Language overrides apply to
this call, and declared model language limits are checked before loading. CPU speech supports
English; audio models depend on the selected models. Hermes manages its own reply instructions.

The transcript updates recognition text when the engine supports partial results and groups spoken
reply segments into one assistant message per turn. Interruption marks the affected reply and
ignores late segments. Text and JSON exports preserve original words, language labels, timestamps
and interruption status. **New conversation** clears the tab's transcript after a call ends.
The transcript stays in page memory until navigation or reload; export it to keep a copy.

**Conversation preferences** contains speaking speed, reply length, instructions, the microphone,
gain, playback volume, pause before reply and echo cancellation. Gain and playback volume can
change during a call; other choices stay locked. Device names appear only when the browser permits
them. **Record this call** explicitly retains audio in SoundRoom for saved-voice calls; unrecorded
audio stays in memory. Startup errors remain visible, and settings failures offer Retry settings.
Canceling startup stops microphone capture and the pending reservation request. Server defaults,
normal request logging and the existing gateway/session authorization remain in effect.

The speech service account needs `hold` and `load`, plus `high` when a speech profile requests
high priority. On this installation these required permissions were restored on 2026-10-25,
preserving the existing request limit; the cloned-voice profile's stale model name was corrected
to `qwen-2.5-1.5b`. Global defaults remain under **System → Voice**.

Deployment check, 2026-10-25: live CPU and saved-synthetic-voice calls each transcribed generated
input and returned a complete model reply as PCM at 1.25× speed; test voice/reference cleanup
succeeded, and no abandoned startup requests remained queued. Browser checks cover call choices,
device/gain/volume controls and retained failure reasons. Personal spoken-chat acceptance remains
in the [delivery ledger](../docs/agent-status.md#open-delivery-and-qualification).

## Latency diagnostics

Milestone 100 is implemented locally **2026-10-15**, with deployment and real latency
qualification pending. System → Voice shows per-turn numeric stages and p50/p95 grouped by
source, mode, profile/model revision and explicit/inferred endpoint basis. The private gateway
keeps at most 200 turns/24 hours in memory since start; numeric `voice.latency` events also
use the existing service journals. No utterance/audio/key/telephone-number capture is added.
Compute reports receive-endpoint/recognition/phrase/synthesis/first-PCM stages on its clock;
control node measures playback progress and client application queue-clear acknowledgements on its
clock. Missing acknowledgements are unavailable; neither an acknowledgement nor PCM sent is
physical speaker-stop proof. New clients negotiate queue-clear reports through the gateway's
`queue_clear_ack` capability. Bounded H1/V7 two-call qualification is complete. The owner closed the current latency gate on 2026-10-16
under [revised targets and bounded recorded-sample acceptance](../docs/voice.md#h1-owner-latency-acceptance--2026-10-16). All 910 local tests passed with zero skips in 482.58 seconds, including
PostgreSQL and desktop/mobile Chromium; one existing Starlette/httpx warning remains.
See the [diagnostic contract](../docs/voice.md#voice-latency-diagnostics-c06-milestone-100).

## Key ports

| Host | Port | Purpose |
|---|---:|---|
| Control node | 443 | Tailscale Serve HTTPS → private browser listener |
| Control node | 8440 | WeightRoom and desktop client download |
| Control node | 8442 | Turnstile to LoadCoach: authentication and model requests |
| Control node | 8445 | Turnstile to compute voice: readiness and session preparation |
| Control node, localhost only | 8446 | Browser voice interface |
| Control node, LAN | 8447 | Desktop voice WebSocket using an API key |
| Control node | 8081 | Twilio voice webhook and audio stream |
| Compute | 8442 | LoadCoach model API |
| Compute | 8444 | Spotter compute/power management |
| Compute | 8445 | Speech service |
| Public HTTPS route | 443 | Reverse proxy, Tailscale Funnel or Cloudflare Tunnel |

## Key endpoints

Relative browser paths are served through the private browser listener; relative voice
service paths are on compute port 8445. The model endpoint is on LoadCoach port 8442.

| Address or path | Purpose |
|---|---|
| `https://control.your-tailnet.ts.net/` | Browser interface; Tailscale Serve is configured, and clients need tailnet access |
| `http://control-node.lan:8440/static/LoadCoach-Voice.zip` | Desktop client download |
| `ws://control-node.lan:8447/desktop/stream?conversation_mode=direct` | Desktop connection; Bearer API key; also accepts `hermes` |
| `POST /browser/sessions` | Browser sign-in and session creation |
| `WS /browser/sessions/{sid}/stream` | Browser audio connection |
| `POST https://voice.example.com/voice/inbound` | Signed Twilio inbound-call webhook |
| `wss://voice.example.com/voice/stream` | Signed Twilio audio stream |
| `GET /voice/ready` | Compute speech readiness |
| `POST /voice/sessions` / `DELETE /voice/sessions/{sid}` | Create or close a compute voice session |
| `WS /voice/stream?session_id=…` | Internal authenticated audio stream |
| `POST /v1/chat/completions` | LoadCoach model inference |

Desktop clients connect directly over the LAN without Tailscale. Browser microphone access
requires HTTPS (or localhost). Internal audio uses mono PCM at **16 kHz input / 24 kHz
output**; the gateway converts Twilio's **8 kHz μ-law** audio.

See the [implementation guide](../docs/loadcoach.md#25-voice) for setup/measurements, the
[voice specification](../docs/voice.md) for requirements and the
[acceptance ledger](../docs/agent-status.md#voice-acceptance) for recorded checks and limits.

## Echo cancellation and spoken interruption

Echo cancellation is enabled by default without pausing the microphone. Desktop uses WebRTC
AEC3 against actual speaker output; browser and phone-browser playback uses a local WebRTC
connection so the browser can cancel it; telephone calls use AEC3 at the control node gateway before
recognition. Browser and desktop offer an Echo cancellation switch, which can be disabled
when using headphones. Speak over a reply to interrupt it. See the implementation guide for
simulated-audio verification. The owner confirmed V9 physical-device qualification complete
on 2026-10-14; recorded acceptance and limits are in the
[agent ledger](../docs/agent-status.md#voice-acceptance).

The greetings follow the speaker selected in **Settings → voice**. Compute regenerates a
complete set when the speaker changes; control node refreshes its persistent `data/voice-prompts.json`
cache through authenticated `GET /voice/prompts` on compute port **8445**. Allow roughly
20 seconds plus synthesis time. The last complete set stays available while compute is off.
Current fallback recordings use **26 — bm_george**. No desktop update is needed for speaker changes.
