# Image extraction implementation plan

Implemented 2026-11-13: design 01 Extraction desk with the optional design 03 batch inbox, real inference, review/history, workflow APIs, caller-bound MCP tools and a stateless Responses adapter. Qualified in an isolated Docker development cluster with actual GPU inference. Production was not updated or restarted.

## Using Image extraction

Open **Chat → Image extraction**, immediately below Image generation. Choose a permitted vision model, upload/drop/paste PNG, JPEG or WebP, select a task and output format, then Run extraction. Multiple uploads open the Batch inbox. Each image keeps its own settings and draft corrections; Run batch submits independent jobs and reports each outcome. Changing settings marks existing results stale until rerun.

Compare the source and result, correct receipt fields or the result's JSON, then Save corrections. Mark reviewed records the human decision while retaining warnings about unknown or inconsistent values. Copy/download uses the selected format; Original JSON exports the immutable first result. History reopens retained sources/results. Presets save task/model/options without an image. Remove uses the shared confirmation and deletes that source's retained jobs. “Use in API or tools” exposes the owner-private source ID.

Models must offer both vision and JSON Schema. Local llama.cpp models require the correct projector; image-generation models are excluded. Administrators explicitly select **Vision + JSON Schema** for a hosted Cloud APIs model mapping after verifying provider support. The picker identifies local/cloud providers and warns that roles may resolve to configured cloud candidates. A selected role still passes normal per-candidate vision/schema admission.

## Tasks and output

| Task | Version 1 result | Formats |
|---|---|---|
| Describe | `description`, `objects`, `visible_text` | Plain, layout, Markdown, JSON |
| Text | `lines` in reading order, `layout_rows` of column cells, `markdown_lines` | Plain, layout, Markdown, JSON |
| Receipt | Nullable merchant/date/currency/payment type/last four; items with description/quantity/unit price/amount; subtotal/discount/tax/tip/total | Plain, layout, Markdown, JSON, CSV |
| Table | Ordered `columns`, string/null `rows` | Plain, layout, Markdown, JSON, CSV |
| Custom fields | Caller-defined closed JSON object schema | Plain, layout, Markdown, JSON |

The service generates versioned prompts/schemas in `weightroom/extraction.py`. All extraction jobs request structured output, then validate it before marking success. Unknown nullable values remain null. Money and quantities use decimal strings; reconciliation independently checks quantity × unit price, items and totals, including refunds. It never fills an unreadable total or currency by calculation or guessing.

Plain text joins the `lines` array. Layout text pads `layout_rows` cells into a monospace grid with preserved row breaks; it approximates alignment, not typography. Markdown uses semantic lines and a renderer with HTML and images disabled. JSON contains the task object. Receipt CSV repeats receipt metadata on each item row; table CSV follows column order. CSV cells beginning with spreadsheet formula markers are prefixed with an apostrophe while raw JSON stays unchanged.

No model in this delivery supplies measured OCR geometry: live `blocks` is empty and provenance says `coordinate_source: unavailable`. The side-by-side source view remains available. Preview highlight boxes are authored fixtures only. No confidence percentages or accuracy guarantees are invented. PDF rasterization, crop/deskew, translation and multi-page merging remain outside this version.

## Ownership and inference

| Responsibility | Canonical implementation |
|---|---|
| Schemas, prompts, raster validation, deterministic exports and warnings | `weightroom/extraction.py` |
| Private assets/jobs/revisions/presets in front's existing accounts database | `weightroom/extraction_store.py`, `db/schemas.py` stream `weightroom.extractions` |
| Async orchestration and authenticated workflow routes | `turnstile/extractions.py` |
| Browser session proxy and live page | `weightroom/extraction_views.py`, `extraction.html`, `extraction_parts.html`, `extraction.js`, `extraction.css` |
| Stateless Responses conversion | `turnstile/responses.py` |
| Signed caller-bound MCP adapter | `toollocker/extraction.py`, `toollocker/door.py` |

Inference always traverses authenticated Turnstile → normal local LoadCoach or configured hosted routing. Extraction never calls llama-server directly and owns no GPU scheduler. Existing account/group access, allowances, placement, sensitivity, cancellation and request accounting apply. Local direct-model jobs disable thinking. Images are treated as untrusted data, without recursive tools. Original model output, validated results and human correction revisions remain separate.

Provenance records the source digest, prompt/schema versions, preprocessing, requested/resolved model, node, model identity, build/fingerprint and request ID where the backend supplies them. Usage comes from the normal inference response. Cloud credentials stay in Cloud APIs; no shared administrator key is embedded in tools.

## Workflow APIs

These authenticated front gateway routes are OWG extensions, separate from OpenAI inference. The browser uses the narrowly scoped session/CSRF proxy `/chat/extraction/api/...`. Every lookup is bound to the stable caller identity; opaque IDs alone grant no access. Responses/downloads use `Cache-Control: private, no-store`.

| Route | Contract |
|---|---|
| `GET /owg/extraction-models` | Authorized vision/schema models and qualified roles |
| `POST /owg/extraction-assets` | One multipart `image`/`file`, or JSON `{name,data}` with base64 bytes → asset metadata/ID |
| `GET /owg/extraction-assets/{id}` | Original raster; `?prepared=1` returns metadata-free oriented PNG |
| `DELETE /owg/extraction-assets/{id}` | Delete unreferenced owned source; refuse retained job references |
| `POST /owg/extractions` | Spec below → 202 job; optional `Idempotency-Key` prevents duplicate submission |
| `GET /owg/extractions?cursor=...` | Owned jobs, up to 50 per page, `next_cursor` |
| `GET, DELETE /owg/extractions/{id}` | Inspect or delete a terminal owned job; final reference deletion removes source |
| `POST /owg/extractions/{id}/cancel` | Idempotently cancel pending/in-flight work |
| `PATCH /owg/extractions/{id}/review` | `{revision,data,reviewed}`; concurrent/stale revisions return 409 |
| `GET /owg/extractions/{id}/export?format=...` | Typed download; `&revision=original` selects revision 1 |
| `GET, POST /owg/extraction-presets` | List or save `{name,spec}` without source/retry ID |
| `DELETE /owg/extraction-presets/{id}` | Remove owned preset |
| `POST /owg/extraction-tools/{tool}` | Internal signed ToolLocker delegation only |

An extraction spec contains required `asset_id`/`model`, `task` (`describe`, `text`, `receipt`, `table`, `custom`), `output_format` (`plain`, `layout`, `markdown`, `json`, `csv`), optional `node`, `instructions` (4,000 characters), `detail` (`concise`/`detailed`), `keep_breaks`, `mark_unclear`, `temporary`, `retry_of`, and `schema` for custom fields. CSV requires receipt/table. Custom schema is limited to 16 KiB/eight levels, requires closed objects with all properties required, and permits a documented primitive/object/array subset; references and arbitrary regexes are rejected. Nullable fields express missing values.

Job envelopes contain `id`, `asset_id`, `asset`, `status`, `spec`, `task`, `output_format`, `data`, rendered `text`, safe `preview_html`, immutable `raw`, `blocks`, `warnings`, `error`, `provenance`, `usage`, `revision`, `reviewed`, timestamps and `status_url`. States are `queued → running → completed|needs_review|failed|cancelled`. Refusal, truncation, invalid JSON/schema, admission failure, timeout and disconnection remain failures with explicit codes. There is no automatic repair or retry that could charge twice.

## OpenAI-compatible endpoints

Existing `POST /v1/chat/completions` accepts ordinary text plus inline `image_url` data and `response_format: {type: json_schema, json_schema: {name,strict:true,schema}}` on capable backends. The JSON object is serialized in `choices[0].message.content`; layout and Markdown are task instructions/rendering, not new OpenAI enums. `json_object` is weaker than schema mode and is never substituted silently for strict extraction.

`POST /v1/responses` now implements an explicit stateless, non-streaming creation subset over the same chat admission path. It accepts message `input_text` and inline PNG/JPEG/WebP `input_image`, string input, instructions, sampling/token limits and `text.format` (`text`, `json_object`, `json_schema`). Use `store:false`, `stream:false`. Output follows the Responses message/output_text/usage envelope and works with the OpenAI Python SDK's `response.output_text`. State, retrieval/deletion, tools, previous IDs, file IDs, remote image URLs, SSE and WebSockets are rejected/unsupported. This is not a full Responses lifecycle implementation.

Existing `/v1/files` + `/v1/batches` accepts JSONL targeting `/v1/chat/completions`, including vision/schema requests. SDK upload/submit/poll/download was verified with one receipt. UI batches instead submit bounded independent `/owg/extractions` jobs; custom OWG jobs are not OpenAI Batch lines. Description/OCR does not use image generation/edit endpoints.

## MCP tools

ToolLocker registers `extraction__describe_image`, `extraction__extract_image_text`, `extraction__extract_receipt`, `extraction__extract_image_table`, `extraction__extract_image_fields`, `extraction__extraction_status` and `extraction__cancel_extraction`. Tasks accept an owned asset ID, permitted model and scalar options; custom fields accepts bounded `schema_json`. Results carry job ID/status, bounded summary, warnings, usage and authenticated status/export references.

The adapter forwards a verified request-scoped caller proof, not a user-supplied identity or shared admin key. Front rechecks the original API key, identity, app restrictions, sensitivity and tool visibility; replay/foreign-owner/revoked-key tests cover this boundary. `extraction_url` defaults to the configured front gateway when ToolLocker loads front configuration. An existing configured MCP server named `extraction` is preserved rather than overwritten.

## Storage and recovery

Uploads allow PNG/JPEG/WebP up to 5 MiB and 16 megapixels. Actual bytes are verified; animated, malformed, SVG, PDF and archive inputs are rejected. EXIF orientation is applied to a metadata-free PNG derivative; alpha is composited on white. The immutable original remains separately downloadable. Remote URL ingestion is unsupported.

Records live in front's existing backed-up accounts database, using migrations 1–2 of `weightroom.extractions`. No extra deployment store or source override is needed. Limits per account: 100 sources/100 MiB of base64-encoded original plus derived data, 200 jobs, 20 pending jobs and 30 presets. Orchestration runs at most two jobs per caller/four globally, with a 900-second bound; normal inference admission still controls resources. Structured output is bounded to 256 KiB.

Abandoned sources and temporary jobs expire after 24 hours; ordinary jobs after 30 days. A reused source remains until its longest retained job expires. Explicit deletion and account deletion remove owned records; cleanup runs with normal front maintenance. Corrections use revision checks and never overwrite the first output. Downloads require authentication. Browser drafts stay in page memory, not local storage. Normal request logging policy still applies; the feature adds no image/OCR logging.

On gateway restart, pending/running jobs become failed with `interrupted`: upstream outcome may be unknown and nothing is replayed automatically. Retry explicitly creates a new linked job. Existing database inventory/backups include these tables. Use the established backup/restore tooling for recovery; preserve accounts, model files and keys. Release through **`owg update; owg restart`** and verify effective running source/live behavior after authorized production deployment. This task verified Docker development behavior only.

## Design previews

The three self-contained proposals remain available through `rtk proxy .venv/bin/python scripts/preview-extraction-designs.py`, then `build/extraction-designs/index.html`. They share `extraction_parts.html`, `parts.html`, `navigation_parts.html` and active palette tokens. Preview operations are browser-local fixtures; uploads display locally without fabricated inference. Reload resets the preview. Designs 02 and 03 remain comparison layouts; live uses 01 plus 03's inbox.

## Qualification and acceptance

All implementation work packages are complete for this delivery: contracts/validation, single-image execution, review/persistence, APIs/tools, mixed inbox batches, stateless Responses and development release qualification. No configured cloud account was available in the isolated cluster: cloud capability persistence/routing was fixture-tested, not live-provider qualified.

On 2026-11-13, actual Docker Chromium and APIs used stored **Gemma 4 12B IT Q4_K_M** with its **F16 projector**, installed through the app's normal model-install API into the disposable node cache. No models were downloaded. Storage sources stayed read-only:

- `/mnt/hdd/llm/gemma-4-12b-it-Q4_K_M.gguf` (7,121,861,440 bytes)
- `/mnt/hdd/models/projectors/gemma4-12b-mmproj-F16.gguf` (175,115,840 bytes)

The actual runtime used an NVIDIA RTX 5060 Ti, about 9.1 GiB resident. The native build reports `0.8.0-dev (build 0, commit unknown)`; qualification records actual launcher/shared-library hashes instead of claiming a source commit. Model identity/fingerprint, usage, source hashes, screenshots and outcomes are generated under ignored `build/extraction-live/{results.json,recovery.json,...}`.

| Check | Observed result |
|---|---|
| Feature, design, Cloud APIs tests | 50 passed |
| Final extraction/backend/browser/prototype/component/docs tests | 50 passed concurrently; 11 preview/docs browser tests also passed after fixing page focus |
| Database migration/consolidation/backup tests | 84 passed |
| Full shared UI audit | 32 passed; 7 failures also present on untouched HEAD: legacy status-copy assertion and six `/backups` raw-None checks |
| Broader existing suites | Turnstile/tools/roles fixture failures reproduced on untouched HEAD; no new failure differences. Repository-wide source-docstring audit still reports existing undocumented definitions; all new extraction production definitions have docstrings. |
| Actual live tasks | Receipt, description, two-column layout text, table and custom schema; all applicable exports; corrections/original revision, presets/history, mixed batch and cancellation |
| Actual compatible clients | OpenAI Python SDK Chat Completions, Responses/output_text and Files/Batch; all seven MCP tools discoverable, signed receipt/status executed; role resolved correct model/node |
| Actual browser | Light/dark at 1600/1024/760/390/320 px, no JS errors/overflow; navigation ordering verified |
| Crash recovery | SIGKILL disposable gateway during running job → failed/interrupted, source retained, no replay; explicit retry completed |

Synthetic clear receipt checks matched merchant/date/currency/three items/quantities/subtotal/tax/total. Rotated 90° and blurred receipts each matched all three selected merchant/tax/total fields. French refund matched those three selected fields and negative values, but returned currency null despite printed CAD; a review warning remained visible. Blank source returned empty text with `no_visible_text`. This small synthetic corpus is qualification evidence, not a general OCR accuracy benchmark; character/word error and real-document accuracy have not been established.

### Reproduce the Docker qualification

Requires Linux Docker, the mounted NVIDIA driver/CUDA toolkit, compatible llama.cpp source, two exposed NVIDIA device nodes and the stored model/projector above. `prepare.py` builds the dev image and native CUDA runtime in an external Docker cache volume; it does not change host runtime installation. The compose file mounts the current checkout read-only at `/app`, uses separate front/compute volumes and localhost ports 20440/20442/20443. Native build defaults target the tested GPU architecture 120.

```bash
rtk proxy .venv/bin/python tests/docker/extraction/prepare.py --llama-source /path/to/llama.cpp
rtk proxy docker compose -f tests/docker/extraction/compose.yaml up -d
```

Open `http://localhost:20440` (disposable test account `operator`, password `extraction-docker-test`) and use Models to install `extraction-gemma` on `alpha` from stored weights and projector. Equivalent authenticated `POST /turnstile/models/install` body:

```json
{"name":"extraction-gemma","machine":"alpha","settings":{"flags":{"model":"/state/alpha/storage/llm/gemma-4-12b-it-Q4_K_M.gguf","mmproj":"/state/alpha/storage/projectors/gemma4-12b-mmproj-F16.gguf","ctx-size":8192,"n-gpu-layers":99,"flash-attn":"on"},"gpus":1,"min_context":4096}}
```

Wait for the normal model-install job to complete, then run:

```bash
rtk proxy docker compose -f tests/docker/extraction/compose.yaml run --rm --no-deps browser
rtk proxy .venv/bin/python tests/docker/extraction/recovery.py
rtk proxy docker compose -f tests/docker/extraction/compose.yaml down -v
```

Set `OWG_EXTRACTION_MODEL_STORAGE`/`OWG_EXTRACTION_PROJECTOR_STORAGE` to relocate read-only storage mounts. The browser harness clears only this disposable account's extraction jobs/presets before each run. Recovery deliberately SIGKILLs only this compose project's gateway. Teardown deletes disposable data/cache volumes and keeps the external compiled runtime cache; it never touches production services or original stored models. Test credentials are generated only inside disposable volumes and must not be used for a public deployment.

## Official API references

Checked 2026-11-13 against official documentation; configured backends still require qualification:

- [Images and vision](https://developers.openai.com/api/docs/guides/images-vision?api-mode=chat)
- [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat)
- [Create a response](https://developers.openai.com/api/reference/resources/responses/methods/create)
