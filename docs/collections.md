# Private document collections

C08 / README milestone 105 is deployed; the owner accepted the H3 corrected-fact and
missing-information answers/citations on **2026-10-16**, as recorded below. The implementation uses WeightRoom's existing process,
private `weightroom.chat` database, account identities and ordinary authenticated model APIs.
There is no new service, database, listener, shared inference key or vector extension.

## Owner grounding review — 2026-10-16

Owner accepted the H3 corrected-facts answer and citation: `qwen3.6-35b-a3b-q4` returned
Harbour Studio Room 2 and CAD 35 from `cedar-workshop.txt`, correctly ignoring obsolete Room 1 /
CAD 30. Collection `a0be4db77ba2593fe8633f5b3e14dea0`; request receipts `45185`, `45187`.
Missing-information observation: threshold 0.65 returned no evidence (request `45198`);
at 0.40 the matching chunk scored 0.647 and the model answered that neither catering nor
parking was confirmed (receipts `45201`, `45203`); the owner accepted that answer and citation.
The [workflow review](workflows.md#live-h3-review--2026-10-16) is also accepted; image review
is [owner-accepted with model-specific limits recorded](loadcoach.md#h3-image-review--2026-10-16).

## Using the page

Open **Benchmark → Collections**. Create a named collection with an explicit local embedding model
and a sensitivity level your current group permits. Upload UTF-8 text or a selectable-text PDF,
then select **Index / retry**. Uploading and viewing status never start inference or wake
compute. A selected collection omits the list-page introduction and return link; its collapsed
embedding-space metadata is at the bottom of the page. While indexing is active, the page refreshes document status, chunk counts and
Index/Cancel controls automatically every two seconds, then stops at completion. Typed questions,
answer/model choices, uploaded-file inputs and displayed answers stay in place; updates defer
for a focused document control or selected document text. A live status line shows completion
or temporary connection failure; background tabs pause polling and failed reads retry with
bounded backoff. **Refresh status** remains a manual fallback. Cancel interrupts ingestion; a retry explicitly starts a fresh
source generation. Restarts retain sources and completed chunks but never replay credentials
or abandoned ingestion automatically.

Search uses the collection's pinned embedder and returns up to three excerpts meeting an
explicit cosine threshold (default 0.65). Similarity is not answer confidence. Start by inspecting
retrieved excerpts, then optionally choose a permitted local text model to answer from them.
No indexed sources means no inference; no relevant hits means an embedding request but **no
answer-model request**. A cited answer must return a bounded JSON answer and references to
provided chunk IDs. Invalid or invented citations are refused. The model may abstain.
Structural citation validation does not prove every claim is grounded; qualify that with real
models and representative questions. Source excerpts are treated as untrusted data. No tool
or model-generated code is executed.

Source links show the exact retained chunk, its PDF page or text line, original SHA-256 and
index generation, and provide an attachment download of the original. Request IDs link to the
existing request diagnostics; their normal permissions and retention still apply. Collection
queries themselves do not create a second conversation history.

## Permissions and embedding spaces

Collections are owner-only in this first increment. Every operation resolves current identity
and permissions through the existing `/loadcoach/me` authority. The owner is the immutable
account identity: renaming preserves access and a recreated account does not inherit it.
Administrators do not gain another user's source access through collection APIs or pages.
Group sensitivity reductions prevent reading or using higher-level collections. Ownership and
active source generations are checked again after asynchronous retrieval/generation. Deleting
an account cancels its ingestion and removes its active collections through the existing
front-side account cleanup hook.

Only catalogue entries owned by local LoadCoach, with ready identity/fingerprint/build and
advertised guarded inference, are eligible. Hosted models, roles and plugin adaptors are not
collection inference destinations. Each request uses the caller's key, collection sensitivity
and trace, and `x-turnstile-no-wake: 1`. Normal admission, cancellation, usage and request logs
remain authoritative. There is no separate GPU scheduler or accounting path.

The embedding space pins model identity, configuration fingerprint, server build and vector
dimension. Model renames preserve the space; changed settings/build or replacement identities
require an explicit **Rebuild embedding space** operation. Rebuild cancels indexing, clears
vectors and marks originals pending; index each source again afterward. Originals remain
available, so an operator can choose the former embedder and reindex to return to that space.
No automatic conversion or mixing of vectors is attempted. Use a fixed-context embedder and
an endpoint that accepts unprefixed text with one pooled vector per input; model-specific
prefixes and token-wise pooling are not configured by this increment.

### Guarded local inference

Matching compute advertises `local_model_guard: true` on local `/v1/models` entries. Embedders
also expose `context_tokens`, the configured minimum request window after parallel-slot caps.
Requests carry these three headers together:

- `X-LoadCoach-Require-Model-ID`: the immutable local model identity.
- `X-LoadCoach-Require-Fingerprint`: the required serving fingerprint.
- `X-LoadCoach-Require-Build`: the required native server build.

LoadCoach checks the configured local identity before routing, refuses hosted destinations and
any hosted offload header, then checks actual server marks again after admission and before
sending inference. Mismatches return 409; incomplete headers return 400. A refused admitted
ticket is released normally. Headers are never forwarded to the native model server. The
client also validates response identity/build, dimensions and finite complete vectors. This
prevents a local model name later reused by a hosted service from becoming an egress path.
A serving context/kin fingerprint different from the pinned one is refused; configure a fixed
matching model rather than accepting a mixed space. Old compute without this advertised guard
is refused before collection inference. Other ordinary API clients are unchanged when they
omit these optional guards.

## Resource and format limits

- Five collections per account, twenty documents per collection, 500 active chunks per collection.
- At most 1 MiB per original; UTF-8 text and total extracted PDF text at most 200 KiB.
- PDFs: one to forty pages; reject encryption, malformed content and every page without
  selectable text, including blank pages. No OCR, URL ingestion, archive extraction or silent
  page/text truncation.
- The fixed PDF parser runs in a child Python process on Linux, with 384 MiB address space,
  five CPU seconds, a ten-second parent timeout, no core dumps/file writes and 32 open files.
  It parses data with pypdf; this is a bounded parser process, not a sandbox for executing code.
  Missing parser support leaves a visible failed document; plain-text sources still work.
- UTF-8 chunks retain page/line provenance, overlap by up to 200 bytes and use at most 1,200
  bytes, reduced conservatively for smaller embedding contexts. No source may produce over
  256 chunks. Inputs are conservatively byte-bounded; native token/context qualification is
  still required for the chosen model. Overflow fails visibly rather than indexing a prefix.
- Vectors must have 1–4096 finite numeric dimensions and nonzero norm. They are normalized
  for cosine search. Different dimensions, nonfinite values and token-vector matrices fail.
- Two ingestion jobs and two searches may run concurrently on front; at most twenty ingestion
  jobs are retained in memory. These limits bound front resources, not GPU placement. An
  ingestion job has a fifteen-minute total deadline including its queue wait; each inference
  request has a ninety-second timeout and a 1 MiB response ceiling.
- Questions are at most 500 characters and must fit the embedder's conservative byte budget.
  Answer requests use at most 512 output tokens and reject insufficient conservative context.

## Storage, deletion and deployment

`weightroom.chat` migration 3 adds `collections`, `documents` and `document_chunks`, with
portable SQLite/PostgreSQL definitions. Originals are base64 TEXT, vectors JSON TEXT. Existing
private backup/restore, integrity and cross-engine transfer paths cover these tables. No new
DSN, store binding or pgvector extension is needed. The old chat-only compute-to-front
copy/retirement tool refuses a source with populated collection tables; use the full store
backup/transfer workflow instead. This bounded first implementation scans
at most 500 vectors per collection in process; a larger corpus needs a separately selected
storage/performance change.

Index publication is atomic. A collection revision, document generation and current ownership
must still match, so deletion, cancellation or rebuild cannot be undone by an old async result.
Source deletion removes its active original and chunks/vectors; collection deletion removes
all its active documents. Earlier answers, accepted workflow outputs, ordinary inference logs and recovery backups keep
their existing retention rules. Deleting a source does not silently rewrite historical logs.

Install the front extra `.[knowledge]` (pypdf **6.20.1**) or use the existing installer/deployer,
which includes it whenever WeightRoom is selected. Dev dependencies include the parser for
regression checks. Compute and plugin runtimes do not need pypdf. Deploy matching compute
first for the guarded API capability, then front and its templates/assets; migration 3 runs
on front at startup. Back up the existing chat store through the normal workflow before an
upgrade. Code rollback after new collection data exists must retain the database and its
migration metadata; an older binary does not implement these collection handlers.

Dependency references: [pypdf 6.20.1](https://pypi.org/project/pypdf/6.20.1/) and
[upstream extraction limitations](https://pypdf.readthedocs.io/en/6.20.1/user/extract-text.html).
The [llama-server API](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#post-v1embeddings-openai-compatible-embeddings-api)
documents string input, float encoding and pooled vectors; the tests do not substitute model
quality evidence for that runtime contract.


## Local verification

All 1,023 full-suite tests passed with zero skips in 566.29 seconds, followed by 42 final
focused checks including three additional maintenance, capacity and native-cancellation cases.
The existing maintenance barrier counts accepted ingestion jobs until completion/cancellation,
and refuses new indexing while marked; database cutover cannot silently ignore these jobs.
Known excess chunk capacity fails before more embeddings are requested, with an atomic final
limit still protecting concurrent publications. Native cancellation checks the actual running
admission list, rather than the unrelated list of compatible model kin.

Tests include owner/admin/recreated-account isolation, rename, revocation during work, real
bounded PDF parsing, old-compute refusal, dispatch guards, stale/deleted sources, vector validity,
no-evidence handling, invalid citations, restart/cancel, PostgreSQL transfer, legacy migration
refusal and browser uploads at 1440/390/320 pixels. One existing Starlette/httpx warning remains.
Transport tests use a scripted native stand-in; no real model or production corpus was qualified.
