# Benchmarks

Benchmarks retain repeatable model measurements and their evidence on the control node.
Current user tasks are in the [benchmark guides](../docs/guide/benchmarks/_section.md).
The suite registry, protocol versions and scoring contracts are documented in
[Code references](/docs/code/benchmarks).

## From a selected model to a result

Selecting several models creates a separate run and batch for each model. Only **one
benchmark run** executes at a time. A probe waits for ordinary work to finish, unloads idle
servers and checks that its assigned GPUs are free of foreign compute processes. Timings
are measured inside LoadCoach on compute, excluding the HTTP hop from the control node to compute.

Ordinary requests take priority. They interrupt a probe and return its run to the front of
the benchmark queue. The incomplete sample is retried; completed samples stay saved. Waiting for busy GPUs or
foreground preemption is separate from the native-failure retry limit. The failure count is
stored in the database, so pausing or restarting does not reset it.

| Event | What happens |
|---|---|
| Pause a run or the whole queue | Interrupt the current affected probe; retain samples; wait for manual resume |
| Stop a run or clear the queue | Cancel affected unfinished jobs; retain their evidence |
| Cancel from the Batches page | The benchmark worker observes the cancellation and stops the run |
| WeightRoom restarts | Previously running work returns to the queue and resumes missing samples |
| LoadCoach restarts or becomes unavailable | Wait for compute, retaining completed work |
| Native server reports a memory/context allocation failure | Save the failed fit and its log; Context searches smaller sizes |
| Other native server failure | At most three attempts per sample, with 2/4-second delays; then fail visibly and keep completed samples |
| Model fingerprint, engine or machine changes during a run | Fail the unfinished run and retain its samples; submit a new run |

Task tokens are scoped to one run and survive browser session expiry. The benchmark database
stores those tokens, **not the user's general API/session key**. LoadCoach rechecks the
submitting account's permissions before probes. Paused jobs remain unfinished batches and
currently keep compute awake through the existing batch accounting.

## What the benchmarks measure

Resource suites measure context fit, speed, load time or embedding resources. Quality suites
run deterministic case plans and retain model output plus local checks. Classifier suites
measure the classifier path separately. The registry in `benchmarks/__init__.py` and each
suite's `DEFINITION` / `PROTOCOL` are authoritative for current names, versions, units and
methods; **Manage → Measurement protocol** presents the selected definition.

A resource probe loads a fresh server. Quality suites use their saved execution mode; each
case remains a separate request with its prompt cache off. GPU generation probes require
full model-layer and KV-cache offload. CPU embedding probes use configured pooling and
physical batch limits. Image/audio models have no token context; their supported resource
measurement is load time.

A successful fit establishes a measured configuration, not a quality guarantee. Scores are
formula-derived; compare underlying measurements and frozen settings before ranking models.

## Tool use and recovery {#tool-use-and-recovery-p4}

Tool suites use deterministic simulated tools, not production external services. Retain
arguments, tool results and local checks when investigating failures. A tool-use score does
not establish that an account can access a real ToolLocker server. Expanded tool use and
recovery exercise different case plans; do not combine their raw success counts.

## General conversation probe {#general-conversation-probe-part-4-milestone-101}

Conversation probes use the native template and request path. A rejected native history or
format is recorded as unavailable/unsupported rather than converted into an ordinary wrong
answer. Inspect exact messages, tools, budgets and checks in the saved case evidence.

## Multi-turn consistency {#multi-turn-consistency-part-6-milestone-102}

Multi-turn cases test consistency across supplied conversation histories. Inspect final
answers and individual assertions; a suite aggregate does not establish general memory or
conversation quality. Protocol identity and frozen case content must match before case
regressions are comparable.

## Extraction and retrieval answer room {#extraction-and-retrieval-answer-room-2026-10-16}

Extraction checks requested fields; retrieval checks answers grounded in the supplied source.
Input room, output budget and unsupported native requests are retained with each case. A
missing/unsupported case is distinct from an incorrect supported answer.

## Paired JSON and schema {#paired-json-and-schema-part-7-milestone-103}

The paired suite compares free JSON and constrained-schema requests using matched cases.
Inspect both answers and format-specific failures. A constrained-format rejection is visible
evidence; it does not establish failure of the corresponding free-output request.

## Expanded tool use {#expanded-tool-use-part-8-milestone-104}

Inspect each call, returned result and deterministic check for multi-step tool cases. Frozen
plans and implementation identity keep retries from silently mixing case versions. Follow
[the suite source](/docs/code/benchmarks?module=benchmarks/suites/tool_use.py) for its exact
current cases and checker rules.

## Pages, scores and result identity

Use [Scores and results](../docs/guide/benchmarks/scores.md),
[Node measurements](../docs/guide/benchmarks/node-measurements.md),
[Compare](../docs/guide/benchmarks/compare.md) and
[Manage scoring](../docs/guide/benchmarks/manage.md) for page controls.

Run evidence retains model ID/fingerprint, engine/machine provenance, protocol identity,
frozen configuration, samples and scoring inputs. A renamed or reused model name cannot by
itself prove identity. An obsolete benchmark version remains readable but is unranked.
Settings-changed evidence can still rank and must be interpreted with its frozen settings.

Case regressions require matching benchmark, protocol/dataset/implementation and grader,
then check case content and input/output budgets. Different model fingerprints are allowed.
New failures and improvements are separate from missing, unavailable, unsupported and changed
cases. Incomplete runs and environment differences are labelled. This compares deterministic
checks; it is not a statistical significance claim and runs no new inference.

Compact completed evidence can inform [Roles](../docs/guide/models/roles.md). Publication
excludes prompts, outputs, settings and task credentials. Definition changes rescore saved
samples. A failed publication warns without erasing retained evidence or the accepted compute
snapshot.

## Files and operation

| Location | Responsibility |
| --- | --- |
| `benchmarks/suites/` | Code-defined suites, their protocols and checkers. |
| `benchmarks/quality.py` | Frozen plans, final-answer extraction, grading and summaries. |
| `benchmarks/__init__.py` | Explicit suite registry and implementation identities. |
| `benchmarks/app.py`, `worker.py` | Internal handlers, scoped tasks, queue execution and reconciliation. |
| `benchmarks/store.py`, `scoring.py` | Retained evidence and safe score expressions. |
| Configured WeightRoom data folder | Private benchmark database and scoped task credentials. |

Use one WeightRoom process so one worker owns the queue. Start, pause and stop runs in
**Benchmark → Runs**. Installation owns dependency setup; updates and restarts follow the
existing [installation workflow](../docs/installation.md). Preserve the database, scoped
credentials and completed evidence during maintenance.

Add a suite as a module exposing `DEFINITION`, `PROTOCOL`, `async measure(worker, run, ceiling)`
and `summarize(samples)`, then register it in `__init__.py`. Quality modules also provide
`grade(case, answer)`. Use existing `worker.probe`, `worker.evaluate` and `quality.measure`
contracts instead of adding an independent execution path. Raise the protocol version when
how the model is asked changes. Imported user case sets are not supported.

Progress comes from saved samples and frozen plans. Adaptive context search reports completed
probes without inventing a fixed percentage. Elapsed time includes waits and pauses; remaining
time is an estimate that excludes future queue waits and unmeasured overhead. A completed run
is the only state that reaches complete progress.

Current developer contracts are in [Code references](/docs/code/benchmarks). Historical
qualification and delivery records remain in the existing evidence documents; they are not
claims about a newly configured model or current runtime.
