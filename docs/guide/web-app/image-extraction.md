<!--docs
title = "Image extraction"
summary = "Describe images, extract text and receipts, review corrections and run independent image batches."
order = 30
covers = ["/chat/extraction"]
-->
# Image extraction

Turn a source image into text or structured data, then check the result against the image.

## Before you start

Use a permitted vision model with JSON Schema support. Upload limits are listed in the [Settings reference](/docs/operations/settings-reference); ask an administrator if an upload is refused.

## Do it

1. Open **Image extraction** and select **Choose image**, drop a source, or paste an image. Use PNG, JPEG or WebP.
2. Choose **Task**, **Output format** and **Vision model**, then select **Extract image**.
3. Review **Extraction result** against **Source image**. Correct structured fields and select **Mark reviewed** when ready; use **Copy** or **Download** to export.

## Options

| Control | Use |
|---|---|
| **Task** | **Describe image**, **Extract text**, **Extract receipt**, **Extract table**, or **Custom fields**. |
| **Description detail** | **Concise** or **Detailed** for a description. |
| **Instructions** | Add guidance; it does not replace the task's structured contract. |
| **Field schema · JSON Schema** | For custom fields, use a closed object, require every field and allow null where information may be absent. |
| **Preview** / **Raw output** | Compare readable output with the returned data. |
| **Image inbox** | Keep separate settings per image; **Extract selected images** starts independent jobs. |

## Limits and troubleshooting

Corrections create revisions; the original model output stays available. Review may be refused if another revision was saved meanwhile: reopen the result before correcting again. CSV is offered for receipts and tables. A schema failure is an error, not a verified extraction. Check uncertain fields before relying on them.

## Related

[Collections](/docs/web-app/collections) searches sources. [Tools](/docs/web-app/tools) explains caller-private extraction tools.
