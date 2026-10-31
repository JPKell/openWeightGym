openWeightGym Voice desktop client

Extract this entire folder before running a launcher. The first launch downloads the
small client dependencies into .venv beside it. No recognition or speech models are
downloaded: those remain on the server.

Windows 10/11:
  Install Python 3.13 from python.org with Tcl/Tk and the Python launcher included.
  Double-click Voice.cmd.

macOS 12 Monterey or newer, Intel or Apple Silicon:
  Install Python 3.13 using the universal2 installer from python.org.
  Run Voice.command. If needed, open Terminal in this folder and run:
    sh Voice.command
  Allow microphone access for the application/Terminal when macOS asks.

Linux:
  Install Python 3.13 or newer, Tk, venv and PortAudio using your distribution's packages.
  On Ubuntu with its default Python 3.13 or newer:
    sudo apt install python3-tk python3-venv libportaudio2
  Run:
    sh Voice.command

Connection:
  Server: http://10.77.10.42:8447
  API key: paste your existing LoadCoach key, or choose its existing file.
  Choose direct or hermes, your microphone and speakers, then Start.
  Echo cancellation is on by default. Speak naturally to interrupt an answer;
  the microphone stays live while the speakers play. Playback keeps a short buffer
  to smooth network timing. With headphones you can
  optionally turn Echo cancellation off. Mute and Interrupt remain available.
  If installing over an older copy, run the launcher again: it installs the new
  WebRTC AEC dependency automatically. Restart an already open app to load the update.

The API key needs front-voice and access to the voice profile's model, including load
if loading is needed. Hermes mode additionally needs front-voice-hermes. Turnstile
checks keys against the accounts on front; calls can wake compute when waking is configured.
The server must have its desktop listener enabled. Neither Tailscale nor a tunnel is
needed on the home LAN. HTTP sends the API key and sound unencrypted over that LAN;
use a trusted LAN or a configured HTTPS endpoint for other networks.

The dark and gold window has Conversation and Settings tabs. Set your server, API
key, conversation mode and audio devices in Settings, then press Save settings.
The masked API key saves automatically after typing, when leaving its field, or on
closing the window. It reloads on startup. Import key file copies an existing key
into the app's own file. Clear the key field to remove the saved copy.

The key is stored in voice-desktop.key beside voice-desktop.json:
  Linux:   $XDG_CONFIG_HOME/loadcoach (normally ~/.config/loadcoach)
  Windows: %APPDATA%/loadcoach
  macOS:   ~/Library/Application Support/loadcoach
The key file is plain text, separate from preferences, with owner-only permissions
on Linux/macOS. Windows uses the permissions of your user profile folder.
Disconnect does not revoke your reusable key. Model requests use the suite's
normal logging rules. The desktop does not save audio or the visible transcript.

Linux was tested with generated audio and the actual Tk window. Windows and macOS
dependency wheels were checked where recorded in the suite guide; native device and
installer execution on those operating systems still require testing there.
