<!--docs
title = "Stores and ownership"
summary = "Each database, the machine it lives on and the programs that write it."
order = 10
covers = []
-->
# Stores and ownership

## The stores {#stores}

These are the stores an installation's default settings name. [System → DB](/databases) shows the ones on your machines, including any other `.db` files it finds.

| Machine | Store | Written by, and what it holds |
| --- | --- | --- |
| Control node | `accounts.db` | WeightRoom, Turnstile and ToolLocker: accounts, client access, events, tasks, control node program and ToolLocker settings, tool schemas, schedule grants, and Mirror Wall and SoundRoom library records. |
| Control node | `roles.db` | WeightRoom and Turnstile: roles, benchmark evidence, reliability, and which models each node may serve. |
| Control node | `chat.db` | WeightRoom: conversations, collections, prompt versions and workflows. |
| Control node | `benchmarks.db` | The benchmark worker in WeightRoom: plans, samples and evidence. |
| Control node | `plugins.db` | WeightRoom and Turnstile: plugin use. |
| Control node | `alerts.db` | WeightRoom: alerts and their acknowledgements. |
| Control node | `schedules.db` | WeightRoom: recurring schedules and their occurrences. |
| Each worker | `loadcoach.db` | LoadCoach and Spotter: models, live events and settings, and identities. |
| Each worker | `spotter.db` | Spotter: telemetry history. |

Every worker has its own pair, including a CPU worker that runs on the control node.

## Files beside the stores {#files}

Images and audio that people keep are files under the control node's `data/mirrorwall-assets/` and `data/soundroom-assets/`; their records are in `accounts.db`, and both are part of every recovery set. Temporary and unlogged images and audio are never written to a store, a file or a recovery set.

Model files stay on the worker that owns them, in its model places, and are not part of a recovery set. Files downloaded from Hugging Face or Civitai go into model storage only, never the cache. A model's file reaches the cache only as a copy promoted from storage for use, and cache cleanup removes it again once it is unused.

[Files](/files) shows a location summary and a library matrix. Select files, choose a source and destination, then copy or move them together. Moves keep source copies until destination copies succeed; storage originals stay. Clean unused copies removes eligible cache and compute copies, preserving files in use and files without a storage original. A shared cache/storage directory has no separate cache to clean.
