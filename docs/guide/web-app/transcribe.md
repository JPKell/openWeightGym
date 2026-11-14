<!--docs
title = "Transcribe"
summary = "Upload or record audio and export its transcript."
order = 100
covers = ["/chat/transcribe"]
-->
# Transcribe

Turn recorded speech into text using a permitted listening model.

## Do it

1. Open **Transcribe** and choose **Transcription model**.
2. Select an **Audio** file or **● Record** a new source. Choose **Language** when you need to identify it explicitly.
3. Select **Transcribe**, check **Transcript** against playback, and use **Copy** or an export button.

## Options

| Control | Use |
|---|---|
| **Task → Translate to English** | Appears for a model that advertises translation; the target is English. |
| **Who spoke when** | Add a compatible diarization model to identify speaker turns. |
| **txt**, **json** | Export plain text or structured transcript data. |
| **srt**, **vtt** | Export subtitles when real segment timings are available. |
| **Load older recordings** | Page through retained recordings. |

## Limits and troubleshooting

Transcription, translation, diarization and timing support depend on the selected model. Recording needs browser microphone permission and a secure browser context. Review names and numbers before using a transcript; diarization labels do not establish a person's identity.

## Related

[Voice generation](/docs/web-app/speech) · [Voice chat](/docs/web-app/voice-chat)
