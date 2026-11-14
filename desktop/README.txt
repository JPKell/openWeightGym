# Voice desktop app

Use the desktop client as your microphone and speakers while models run on the server.

## Before you start

Download **Voice desktop app** from the console and extract the entire ZIP. Install the platform requirements below. You need an application key and Voice/model access; use the desktop server address supplied by your administrator. A trusted LAN or configured HTTPS endpoint is required; HTTP sends the key and audio without transport encryption.

## Do it

1. Open the launcher for your platform. The first launch installs client dependencies into `.venv` beside it; model weights stay on the server.
2. In **Settings**, set the server and API key, or choose **Import key file…**. Select **Mode**, microphone and speakers, then **Save settings**.
3. Open **Conversation** and select **Start**. Use **Mute**, **Interrupt**, **Finish speaking** or **Hang up** as needed.

## Platform requirements

| Platform | Requirements and launcher |
|---|---|
| Windows | Python 3.13 with Tcl/Tk and the Python launcher; open `Voice.cmd`. |
| macOS | Python 3.13 universal installer; open `Voice.command` or run `sh Voice.command`. Allow microphone access when asked. |
| Linux | Python 3.13 or newer, Tk, venv and PortAudio; run `sh Voice.command`. |

## Options and privacy

**Echo cancellation** supports speaking while replies play. **Refresh devices** reloads the audio-device choices. Direct mode talks to the configured model; Hermes mode transcribes speech, sends text to Hermes and synthesizes its reply, subject to its additional account access.

The masked key saves after editing or leaving its field, and reloads on startup. Importing copies the key into the app's private credential file. Clearing the key field removes that saved copy. Hanging up does not revoke a reusable key. The key remains plain text in a separate credential file: Linux/macOS use owner-only permissions, while Windows uses the user profile's permissions.

The desktop does not save audio or the visible transcript. Server requests follow normal account logging policy. Native audio-device behavior needs checking on the actual client machine.
