<!--docs
title = "Voice generation"
summary = "Give a script and its speakers voices, then generate and review audio."
order = 90
covers = ["/chat/speech"]
-->
# Voice generation

Create spoken audio from a script using the voices supported by your selected model.

## Do it

1. Open **Voice generation → Generate** and choose **Speech model**.
2. Enter **Your script** or use **Import .txt**. Prefix dialogue lines with `Name:`; **Cast** detects speakers so you can assign their voices.
3. Select **Generate speech**, then listen to the output before exporting or sharing it.

## Options

| Control | Use |
|---|---|
| **Saved voices** / **Default voice** | Select an accessible voice compatible with the speech model. |
| **Built-in voice name** | Use a name that the selected model supports. |
| **Describe the voice** | Appears for models supporting voice design. |
| **Generation settings** | Adjust line pauses, seed and the offered temporary mode. |
| **Model settings** | Use the controls advertised by the selected model. |

## Save a voice

Open **Voices**. For cloning, provide **Name**, **Recording**, and **What the recording says**, confirm **I have the speaker's permission to clone this voice**, then select **Save voice**. **Fill from recording** can use a permitted transcription model. A compatible **Voice-design model** instead offers **Description**, **Sample sentence** and **Design and save**.

## Limits and troubleshooting

Voice, cloning and design capabilities differ between models. A removed or no-longer-shared voice can block a script; reassign its cast entry. Files and generated takes remain subject to current ownership and sharing permissions. Temporary source audio requires a temporary job. Temporary generation is offered only when your account and current compute policy permit it. Upload policy is shown by the workspace; administrators can inspect [shipped defaults](/docs/operations/settings-reference).

Kokoro requires the eSpeak-ng shared library on its compute node. If generation reports that it cannot load eSpeak-ng, an administrator must install the distribution package (`espeak-ng` on Arch, `libespeak-ng1` on Debian/Ubuntu, or `espeak-ng-libs` on Fedora). The audio.cpp build helper checks this dependency; a failed take can be retried after it is installed.

## Related

[Transcribe](/docs/web-app/transcribe) · [Voice chat](/docs/web-app/voice-chat)
