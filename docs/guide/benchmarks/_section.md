<!--docs
title = "Benchmarks"
summary = "How models are measured: what a run does, what each benchmark tests, how scores are kept, and how the benchmark worker is run."
order = 50
audience = "admin"
covers = ["/docs/benchmarks"]
-->
# Benchmarks

## How benchmarking is put together {#benchmarks}

Benchmarks run inside WeightRoom on the control node and use LoadCoach over HTTP. The `benchmarks/` package owns plans, the queue, results and scoring; it has no service, command or port of its own. LoadCoach owns authentication, durable batches, model processes and GPU access, and the benchmark package never opens its database or starts a model itself. Each benchmark lives under `benchmarks/suites/`, in its own Python file or folder.

Saved definitions, runs and results stay readable while LoadCoach is unavailable; starting a run and comparing live model state wait until a compute node answers. Restarting WeightRoom interrupts the probe under way, and the missing samples are resumed when it starts again.

## Where to find it {#benchmark-pages}

[Benchmark → Runs](/benchmarks/runs) starts a run and follows it; Results, Node measurements and Compare show what runs have found. [System → Schedules](/schedules) reruns benchmarks on a schedule, or runs one missing or outdated benchmark at a time.
