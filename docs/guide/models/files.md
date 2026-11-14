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

1. Open **Files** and use the location summary or searchable library matrix to find a file.
2. Select the source and desired destinations before copying or moving. Review transfer progress and errors.
3. Open the individual file to inspect metadata, node copies and its offered configuration actions. **Search metadata** and **Metadata group** narrow the record.

## Limits and troubleshooting

Downloads arrive in storage; active cache copies are promoted for use. A move preserves sources until destination copies succeed, and storage originals stay protected. **Clean unused copies** removes only eligible cache/compute copies backed by storage. Cleanup refuses unavailable usage evidence rather than guessing that a file is unused. A shared cache/storage directory has no separate cache to clear.

Deletion is permanent and requires the current unused-file checks. Check the selected copy and all node usage before confirming it. Metadata is evidence read from the file, not a guarantee that its configuration will load.

## Related

[Downloads](/docs/model-guides/downloads) · [Fingerprints](/docs/model-guides/fingerprints)
