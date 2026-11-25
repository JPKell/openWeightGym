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

In a Direct conversation, **reply length** bounds only the spoken answer. A model that reasons before answering, even when asked not to, has a separate 1,024-token thinking allowance that is never spoken. If its reasoning uses all of that and no answer follows, the assistant says “Sorry, I ran out of time thinking about that. Could you ask it more simply?” instead of asking you to repeat yourself; a model that answers without reasoning avoids the delay.

If **Start** fails, the notice says which step failed and what to do: the browser could not start audio processing, microphone access was denied or no matching microphone was found, call playback could not be prepared, or the voice service refused or could not be reached. The browser's own error name follows in brackets for diagnosis. Nothing is recorded and no session is left open, so you can correct the cause and select **Start** again.

## Related

[Voice desktop app](/docs/web-app/voice-desktop) · [Voice generation](/docs/web-app/speech)
