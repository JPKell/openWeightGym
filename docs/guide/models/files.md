<!--docs
title = "Files"
summary = "Inspect model metadata and manage verified file copies."
order = 80
covers = ["/files"]
-->
# Files

Inspect model metadata and manage verified file copies.

## Before you start

Use an account with [model management](/docs/system/abilities#ability-manage-models). Disabled platform services may hide media and download choices.

## Do it

1. Open **Files** and use the location summary or searchable library matrix to find a file. The file-type pills narrow the matrix; **Video** lists components of video models (Wan, LTX-2, HunyuanVideo and MiniMax-H3), which **Images** then leaves out.
2. Select the source and desired destinations before copying or moving. Review transfer progress and errors. Finished transfers, installs, bulk changes and conversions leave the list a day after they end; **Clear finished** removes them at once and leaves running tasks.
3. Open the individual file to inspect metadata, node copies and its offered configuration actions. **Search metadata** and **Metadata group** narrow the record.

An adapter or projector file offers **Make a model with this adapter** or **Make a model that sees**.
Choose a language model from any node; models the file names come first, and others are marked. The new
model copies the base model's settings on each node that has it, adds the file, and leaves the base as it
was. Where a node does not hold the file yet, such as a file in control-node storage, an ordinary
installation copies it there first; follow its progress on Models.

Compatible HunyuanVideo weights and LTX-2.5 text encoders offer **Convert video component**.
Choose RAM and temporary disk budgets; Files tracks conversion without replacing the source.
See [Video models](/docs/model-guides/video-models) for component requirements and qualification.

## Limits and troubleshooting

Each node has one model storage folder, set in **Settings → Model file locations**; models load from it directly, and there is no separate cache. Every folder in it is a location: the standard `llm`, `adapters`, `projectors`, `image`, `audio`, `encoders` and `classifier`, plus any other folder already there, such as `video`. `video` is searched as deeply as `image`, and its files are read as image and video components. A folder added to a node's root appears after that node restarts; on the control node it appears on the next Refresh. Only recognised model formats are listed, so a folder holding other data shows as an empty location.

The matrix has a **Storage** column for the control node's model storage, when it has one, and a column for each compute node. Downloads arrive in model storage. Adding a model copies its file only to a node that lacks it, into that node's folder of the same kind; a file already on the node is used where it is. A move preserves sources until destination copies succeed. **Clean unused copies** removes only compute-node copies that no model uses and that Storage also holds. Cleanup refuses unavailable usage evidence rather than guessing that a file is unused.

A node that still has the retired second model folder (`models_storage_folder`) set keeps it working: its folders appear as extra locations named `storage_llm`, `storage_image` and so on. Move their files into model storage from each file's page, then clear the setting.

A file's **Used by** counts the models that load it directly and those that load a node copy of it, as the **in use** mark in the list does; for a Storage original the record names the nodes whose copies are used. A file a model loads directly is protected from deletion. Deleting a Storage original whose copies are in use is allowed, labelled **copies in use**: the copies and their models keep working, but a new copy can then only come from downloading the file again.

Deletion is permanent and requires the current unused-file checks. Check the selected copy and all node usage before confirming it. Metadata is evidence read from the file, not a guarantee that its configuration will load.

## Related

[Downloads](/docs/model-guides/downloads) · [Fingerprints](/docs/model-guides/fingerprints)
