<!--docs
title = "Voice chat"
summary = "Talk with permitted speech and language models in your browser."
order = 25
covers = ["/voice"]
-->
# Voice chat

Talk with a model while reading the original transcript and its replies.

## Before you start

Use HTTPS or localhost and allow microphone access. Your account needs [Voice access](/docs/system/abilities#ability-front-voice), permitted speech/language models and [loading access](/docs/system/abilities#ability-load) when a model must load.

## Do it

1. Open **Voice chat**. Choose **Conversation**, **Speech engine**, **Listen · STT model**, **Think · LLM model**, **Speak · TTS model** and a supported **Voice within model**.
2. Select **Start conversation**. Speak naturally; use **Finish speaking** when you want to end an utterance explicitly.
3. Use **Mute mic**, **Interrupt reply** or **End call** as needed. **Copy transcript** or **Export** saves the tab's transcript before leaving.

## Options

| Control | Use |
|---|---|
| **Conversation preferences** | Set speaking speed, reply length, instructions and echo cancellation for this call. |
| **Translate replies** | Choose **I speak / listen in** and **Model responds in**; support depends on the selected speech models. |
| **Record this call in the audio library** | Appears when the selected mode/account supports retained recordings. |
| **New conversation** | Start with a fresh visible transcript. |

## Limits and troubleshooting

Model choices are made before a call. Voice discovery checks configured active nodes without waking them, including nodes whose service telemetry omits Voice; an explicit “not installed” report excludes a node. A missing permission, unavailable profile or occupied voice capacity can refuse a start. If settings fail, the notice distinguishes refused access, missing endpoints, timeouts and unavailable Voice compute. Administrators can open **Components** or **Voice settings** from the notice, restore the dependency, then select **Retry settings**. Export a transcript you need to keep; the visible transcript is tab-local.

## Related

[Voice desktop app](/docs/web-app/voice-desktop) · [Voice generation](/docs/web-app/speech)
