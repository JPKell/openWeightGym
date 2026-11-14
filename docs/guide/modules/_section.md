<!--docs
title = "Modules"
summary = "The programs openWeightGym is made of: what each one does, where it runs, and its code."
order = 40
audience = "admin"
covers = ["/docs/modules"]
-->
# Modules

## Where each one runs {#modules}

The console machine, control node, runs WeightRoom, Turnstile, ToolLocker and, when it is used, the voice gateway. Each GPU or CPU worker runs LoadCoach and Spotter. On a single machine they all run side by side. Control node owns accounts and retained history; workers own model files and their own runtime state.

| Module | Runs on | What it does |
| --- | --- | --- |
| [WeightRoom](/docs/modules/weightroom) | Control node | The web console: pages, chat, collections and workflows, and the benchmark worker. |
| [Turnstile](/docs/modules/turnstile) | Control node | Authentication, model and role placement, hosted calls, and signed routing to workers. |
| [ToolLocker](/docs/modules/toollocker) | Control node | The MCP gateway. File tools run on the hosts that own the files. |
| [Voice](/docs/modules/voice) | Control node and compute | The gateway on the control node takes browser, desktop and telephone connections; the voice service on compute does speech recognition and synthesis. |
| [LoadCoach](/docs/modules/loadcoach) | Each worker | Model processes, measured admission, inference and scheduling. |
| [Spotter](/docs/modules/spotter) | Each host | Telemetry, and the service and power controls it allows. |
| [Plugins](/docs/modules/plugins) | Control node | Adapters that offer external agents and services as models. |
| [Mirror Wall](/docs/modules/mirrorwall) | Control node and compute | Image requests and each person's image library on the control node; generation with stable-diffusion.cpp on compute. |
| [SoundRoom](/docs/modules/soundroom) | Control node and compute | Each person's audio library on the control node; music, sounds, speech and transcription with audio models on compute. |

Model servers listen on private loopback ports; clients reach them through the control node. How the pieces connect is drawn on the [system map](/docs/system#system-map).
