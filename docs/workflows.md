# Versioned prompts and a resumable research-to-draft workflow

C10 / README milestone 106 is deployed; the owner accepted the live H3 research-to-draft
sample on **2026-10-16**, as recorded below.
**Benchmark → Workflows** hosts one fixed, read-only collection-research-to-draft workflow inside
WeightRoom's existing chat process and private database. It reuses C08 retrieval and guarded
ordinary inference; it adds no service, listener, shared credential, GPU scheduler, agent runtime
or tool/code executor.

The existing Hermes bridge was assessed first: it wraps a separate native agent CLI/profile
with its own shared memory/files/credentials and native sessions. It remains available for
that role. These private prompt versions and accepted front-side outputs use ordinary openWeightGym
model APIs so their ownership, source access and accounting remain the current user's.

## Live H3 review — 2026-10-16

Workflow `8416d5432932f04dff1501bcdf9394c4` used `qwen3.6-35b-a3b-q4` and built-in v1
research/draft prompts against the [owner-reviewed Cedar source](collections.md#owner-grounding-review--2026-10-16).
Research completed and was accepted by the agent after comparison with the source (receipts
`45221`, `45224`); draft completed with its source link (receipt `45229`, 593 input / 215 output
tokens). Research usage and local cost remain unavailable after refresh. The owner accepted
the draft and citation; the live workflow now shows completed with Draft · Accepted. Image
review is [owner-accepted with model-specific limits recorded](loadcoach.md#h3-image-review--2026-10-16). No announcement was published or sent.

## Using Workflows

Three coordinated, offline Collections and Workflows proposals are available in the
[design previews](collections.md#design-previews), including sample review gates and retry states.

The live page uses **01 Evaluation desk**: saved runs sit beside the selected run's question,
progress, pinned models/prompts, research and draft. The latest matching run opens by default.
Search and stage filters also work as native GET forms. **New workflow** and **Prompt library**
open native disclosures; unsent form values stay in page memory while switching runs.
Attempts, usage and full frozen configuration remain expandable below the run workspace.

1. Index sources in an owner-private [collection](collections.md).
2. Optionally save writing instructions in **Prompt library**. The same name and kind creates
   the next immutable version. Research and draft versions are separate; built-in v1 prompts
   are available without creating a library entry. Instructions are literal text, not templates
   or executable programs.
3. Create a run with its question, collection, similarity threshold, two permitted local text
   models and two prompt versions. Creating the plan does not start inference. Review the
   frozen configuration, including effective system prompts and output limits.
4. Select **Start research**. Retrieval takes up to three matching excerpts. With no relevant
   evidence, the workflow ends without asking the research/draft model. Otherwise it requests
   a source-bound JSON research note through the existing API and validates citations.
5. Review the research and source links, then **Accept research**. Acceptance makes that exact
   output the input to the draft; it does not certify factual correctness.
6. Select **Start draft**, review its cited output, then **Accept draft** to mark completion.
   A failed/cancelled draft leaves accepted research intact. **Resume / retry draft** starts
   only a new draft attempt; it does not redo accepted research or retrieve different evidence.

Status refresh is manual, so an active selection/form is not periodically replaced. Cancel
stops the front task and uses ordinary request cancellation; it does not promise a physical
model stop latency. After restart, interrupted steps are paused and require a new authenticated
resume. No account key is persisted for automatic replay. A replacement key for the same
immutable account can resume; a recreated account cannot inherit the run.

## Frozen inputs and review boundaries

Workflow protocol v1 records the question, collection revision, similarity threshold, three-hit
limit, prompt version/body/SHA-256, complete effective system prompt/SHA-256, concrete model
identity/fingerprint/build, temperature 0 and output limits (research 1,024, draft 2,048 tokens).
The writing prompt and source/citation rules are both frozen before starting. Library changes
and later default changes cannot silently alter an existing run. Breaking protocol changes
must advance the version and retain the old contract or refuse it explicitly.

The first successful retrieval freezes source chunk IDs, original digests, generations and
locations. The workflow stores references, not another raw-source text/vector copy. A research
retry uses those same references. Each step resolves current source access and exact active
chunks before inference and rechecks them before publishing output. Deletion, reindexing or
embedding-space rebuild blocks stale inputs; a new run is required. Accepted prior output
remains readable as private history even if its source collection is subsequently deleted,
subject to the caller's current sensitivity permissions.

Model renames resolve by immutable identity. Changed generation-model fingerprints/builds,
removed model access or incompatible contexts fail visibly with no fallback. The operator can
retry transient failures or create a new run with a new configuration; no model/prompt is
silently rebound inside an existing plan. Requests use the caller's current key, collection
sensitivity, per-attempt trace and C08's pre-dispatch/post-admission local model guards. Hosted,
role and plugin-adaptor destinations are excluded in this increment. No tool definitions are
sent and no model-selected action or generated code executes.

Research and draft share C08's citation validator. Outputs require an assistant message, no
tool calls, bare unambiguous JSON, typed answer/abstention fields and only supplied source IDs.
Duplicate JSON fields, malformed envelopes and invented citations fail. Structural citations
do not prove semantic grounding; representative real-model questions and operator review are
still required. A model abstention is retained as an unsuccessful attempt without advancing
acceptance. Correctness is not assigned a fabricated numeric score.

## Attempts, usage and cost

Every start/retry has its own step, ordinal, state, timestamps, receipts and outcome. Generation
request IDs and actual model marks are retained as soon as headers arrive, including a later
body failure/cancellation. Fresh retrieval records its ordinary embedding request ID. Existing
request diagnostics remain the detail view and authority; there is no second billing ledger.

Usage is reconciled automatically after each attempt from available normal request records. Trace matching prevents
an unrelated/reused request number from being treated as this attempt's evidence. Native input/
output counts and recorded costs are used only when available and valid. A complete attempt
aggregate requires its generation receipt and known values for all observed requests; a known
retrieval component does not become a complete step total when the generation receipt is missing.
Unpriced local inference, missing final usage and unobserved receipts remain **Unavailable**,
not zero. Reconciliation during a new receipt cannot overwrite a newer receipt set, and an unavailable
read retains previously observed values. Retries keep separate records so earlier work is
not silently erased from the history. Ordinary request/account reporting still owns actual
usage/spending; this feature adds no reservations or hard budget guarantees. C07 now supplies separately
configured soft [allowance reporting](allowances.md), also without reservations.

## Ownership, storage and limits

All APIs use current `/loadcoach/me` identity and sensitivity. Prompt versions and runs belong
to the stable account identity; even administrators cannot open another user's private records
through these handlers. Source permissions are the existing C08 checks. Account deletion cancels
its active jobs, then removes its private runs, attempts, prompt versions and collections through
the existing cleanup path. Explicit workflow deletion removes its saved outputs/attempts; it
leaves source documents and normal logs/backups under their existing retention policy.

`weightroom.chat` migration 4 adds portable SQLite/PostgreSQL `prompt_versions`, `workflows` and
`workflow_attempts` tables in the **same** private store. Existing backups and verified cross-engine
transfer cover them. The old compute-to-front chat-only copy/retirement tool refuses populated
new collection/prompt/workflow tables rather than omit their data.

Limits: 200 private prompt versions, 4,000 instruction characters per version; fifty retained
runs per account; ten attempts per step; twenty in-memory jobs with two active workflow steps.
A step has a five-minute total execution deadline including its queue wait, plus bounded usage
reconciliation (two seconds per observed request). Native inference retains C08's ninety-second
request and 1 MiB response limits. Conservative prompt-byte/context checks refuse oversized plans
before generation. These front resource bounds do not change GPU admission or user/group limits.
Accepted background work participates in the existing maintenance drain until cleanup finishes;
new steps refuse while the installation is marked for database maintenance.

## API and deployment

Internal authenticated handlers beside conversations:

- `GET/POST /loadcoach/workflows`: private list (including prompt versions/built-ins), create plan.
- `POST /loadcoach/workflows/prompts`: create a new immutable prompt version.
- `GET/DELETE /loadcoach/workflows/{id}`: private detail/attempts, delete after cancellation.
- `POST /loadcoach/workflows/{id}`: `action` is `start`, `cancel`, `accept` (with fixed `step`), or
  `usage`. Invalid transitions refuse; a stale epoch cannot publish output into a changed run.

The console reaches these through its existing in-process authenticated ASGI client. No public
Turnstile route/port or credential is added. Deploy matching C08 compute guards and updated front
code/templates; startup applies chat migration 4. There is no new dependency beyond the front
knowledge extra already selected for C08. Preserve the chat store and migration metadata when
rolling code back; older code has no workflow handlers. Real-model grounding, interruption,
receipt/cost availability and restore behaviour require deployment qualification.
