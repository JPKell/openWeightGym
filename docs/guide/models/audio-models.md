<!--docs
title = "Audio models"
summary = "Install audio files and edit their node-specific task settings."
order = 40
covers = ["/models/audio"]
-->
# Audio models

Install audio files and edit their node-specific task settings.

## Before you start

Use an account with [model management](/docs/system/abilities#ability-manage-models). Disabled platform services may hide media and download choices.

## Do it

1. Open **Audio** under **Models**. Choose an installed model and node, or **Add audio model**.
2. In **01 · Model & files**, select **Audio task**, **Model file in cache or storage**, **Model family** and **Model name**.
3. Review **02 · Hardware**, **03 · Output** and **Advanced**. Choose the intended **Install on compute nodes** targets for a new model, then submit the editor.

## Options

Node pills switch between installed copies while retaining drafts. **Additional generation defaults (JSON)** preserves extra saved keys; **Session options (JSON)** follows the selected family's contract. **Discard changes** restores the saved editor state.

## Limits and troubleshooting

Families advertise different music, speech, transcription, translation and diarization capabilities. Choose a family matching the actual files. Required runtime components must be installed on each selected node. Review real installation progress before using the model.

## Related

[Voice generation](/docs/web-app/speech) · [Transcribe](/docs/web-app/transcribe) · [Music and sounds](/docs/web-app/music-sounds)
