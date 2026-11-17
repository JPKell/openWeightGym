<!--docs
title = "Design previews"
summary = "Render standalone sample workspaces for local interface review."
order = 90
covers = []
-->
# Design previews

Render sample proposals locally without contacting live services.

## Do it

1. Run `.venv/bin/python scripts/preview-docs-designs.py` from the repository root.
2. Open `build/docs-designs/index.html` to compare Search-first library, Guided hub and Reference desk.
3. Regenerate after guide or template changes. Previews share the article renderer and discovered catalogue.

Search, section filters, article contents, browser history, light/dark appearance and narrow screens are supported. Code references remain separate. Use `--base-url` to point explicit live-console links at a configured console.

For My account proposals, run `.venv/bin/python scripts/preview-account-designs.py` and open `build/account-designs/index.html`. Sample edits and keys stay in page memory; proposed controls are labelled and no real account changes. Repository `AGENTS.md` lists other preview scripts and owning components.

For Video generation and Video models, run `.venv/bin/python scripts/preview-video-designs.py`
and open `build/video-designs/index.html`. Each tab offers Canvas, Desk and Review layouts with
browser-local sample actions, light/dark appearance and failure states. Sample artwork is
illustrative; it is not a generated video. The selected live layout is Canvas on both pages.

For Image workflows, run `.venv/bin/python scripts/preview-image-workflow-designs.py` and open
`build/image-workflow-designs/index.html`. Node canvas, Stage lanes and Split studio share one
editor: Send to and Use from connect steps, Add after inserts a step, and the test run returns
placeholder pictures with a matching API example. Samples are read-only; duplicate one to edit.
Narrow screens start in Outline. Nothing is generated and no service is contacted.

## Related

[Find your way](/docs/getting-started/navigation), [My account](/docs/web-app/my-account).
