# Classifier (decision) models — work plan

Status: **implemented; hosted acceptance pending** (2026-11-13). Native routing, recognition,
the chat shim, Classifier workspace, cloud mappings, MCP tool and service policy are deployed.
Delivery verification is tracked below. Deployment and all live tests are authorized by the owner.

## 1. What a classifier model is

A decision model reads a state (text, JSON or chat messages) and answers typed questions **by
choosing among answers the caller supplies**. It never writes free text. You can ask it several
questions about one input in a single call, and it returns probabilities instead of generated
tokens:

| Question type | Criteria | Answer |
|---|---|---|
| `choice` — classification | object `{option: description | null}` | `choice`, `probabilities` (sum 1), `confidence` |
| `score` — ordered rating | array of 2–10 levels, lowest first | `score` (expected level, may be fractional), `legend`, `probabilities`, `confidence` |
| `noul` — yes/no probability | optional `{true, false}` descriptions | `noul` (probability that the answer is true) |

Multi‑label tagging is done by asking several `noul` questions in one request. The temperatures
stored in the model file scale the probabilities, but the probabilities are **not calibrated for
the user's data**. The UI and the docs say so.

### The runtime we already have

The configured **llama.cpp b12008** (`runtimes/llamacpp/current/llama-server` in the installed checkout) serves decision models natively
at `POST /v1/systemone` (TypeSafe‑compatible; no streaming; images are optional through
`--mmproj`). A model counts as a decision model only when its GGUF carries
`<arch>.decision.type` metadata and a `systemone` chat template. `GET /v1/models` then reports
`architecture.output_modalities: ["decisions"]` without loading the model. Supported families:
`openjev`, `lev`, `kev`, `nimble`, `laya`, `clef` and `pplx-decider`. Errors are 400 for an
invalid request and 501 for a non‑decision model or unsupported images.

**Owner decision: llama.cpp only.** There is no PyTorch serving engine. Classifiers run on the
existing `llama_cpp` engine, and PyTorch appears only in the optional conversion tool (WP2).

## 2. Decisions from the interview

| Topic | Decision |
|---|---|
| Runtime | Existing llama.cpp engine; native `/v1/systemone` |
| Naming | **Classifier** everywhere: pill, `classifier/` folder, Interact page **Classifier** at `/chat/classifier` |
| Recognition | GGUF decision metadata makes a file a Classifier wherever it is found; downloads and conversions go into `classifier/`, and other files can be moved there from Files |
| Downloads | Native Apache‑2.0 set (§3), all on **one GPU compute node** first |
| Conversion | Optional in‑OWG conversion job; first targets **TinyJev‑0.6B** and **TinyJev‑4B v2** |
| Remote code | Refused: no `trust_remote_code` bundles, and no `.py` files kept in `classifier/` |
| OpenAI compatibility | Native `/v1/systemone` (+ `/v1/classifier` alias) **and** a `/v1/chat/completions` shim |
| Cloud | Add decision providers (TypeSafe Jev, Featherless) as a Cloud APIs model type, with local‑first fallthrough |
| Access | Existing per‑user/group model access, limits and allowances; no new ability |
| History | Page memory with JSON/CSV export (AI logs are unchanged) |
| Page design | Straight to one live design; iterate afterwards |
| First delivery also includes | Decisions/sec benchmark, accuracy suite, MCP classify tool, Platform services switch |

## 3. Open‑weight models to download for implementation

These are all native decision GGUFs published by **ggml-org**, licensed **Apache‑2.0**, and
verified on Hugging Face on 2026‑11‑13. They go into `classifier/<repo>/` on **one GPU compute
node** (about 13 GB). They are copied to other nodes later with Files copy/distribute.

| Family | Repository → file | Size | Why it's in the set |
|---|---|---|---|
| Julia‑1 (laya type, ModernBERT) | `ggml-org/Julia-1-GGUF` → `Julia-1-Q8_0.gguf` | 0.16 GB | Smallest; good for CPU fallback and fast tests; whole prompt must fit in `--ubatch-size` |
| Laya | `ggml-org/Laya-GGUF` → `Laya-Q8_0.gguf` | 0.42 GB | Encoder decision model; up to 255 choices; truncates long text to its trained budget |
| Kev 0.8B (pointer head) | `ggml-org/Kev-0.8B-GGUF` → `Kev-0.8B-Q8_0.gguf` | 0.76 GB | Small Kev; the reference for TinyJev‑0.6B parity |
| Kev 4B | `ggml-org/Kev-4B-GGUF` → `Kev-4B-Q4_K_M.gguf` | 2.83 GB | Mid‑size Kev |
| lev | `ggml-org/lev-GGUF` → `lev-Q4_K_M.gguf` | 2.82 GB | Letter‑logit family; `noul` from a rating scale |
| Kev 9B | `ggml-org/Kev-9B-GGUF` → `Kev-9B-Q4_K_M.gguf` | 5.92 GB | Largest Apache‑2.0 native model; quality ceiling for the accuracy suite |

Inputs for the conversion job (WP2), downloaded as source bundles:

| Source | Files | Size | Conversion route |
|---|---|---|---|
| `AnkitAI/TinyJev-0.6B` (MIT) | `model.safetensors`, `head.safetensors`, `tinyjev.json`, tokenizer/config | 1.1 GB | Kev pointer head on **Qwen3** (llama.cpp's `KevModel` expects a kev LoRA + `head.pt` on Qwen3.5). Needs our adapter: the q/k head tensors, `decision.type=kev`, the systemone template and the temperature from `tinyjev.json`. Gate: answer parity with the `tinyjev` reference package on a fixed question set. |
| `AnkitAI/TinyJev-4B` v2 (Apache‑2.0) | `TinyJev-4B-Q4_K_M.gguf` + `Modelfile` | 2.5 GB | Already GGUF (qwen35) but has **no** decision metadata; it targets Ollama's `/v1/systemone`. Add the metadata and a systemone template matching the Ollama prompt bytes with gguf‑py's metadata rewrite; no re‑quantizing. Gate: parity with Ollama ≥ 0.35.1 on the same questions. |

Not in phase 1, recorded for later: OpenJev / OpenJev‑Flash‑9B and Bespoke‑Nimble‑9B (native
GGUFs, but **CC‑BY‑NC‑4.0**); AutoTrust‑Decider‑Pro‑27B (formerly JEV‑27B; safetensors + its own
head, about 54 GB); pplx‑decider‑v1‑27b (Apache‑2.0, no prebuilt GGUF); nano‑jev (a cross‑encoder,
not a llama.cpp decision type).

Downloads use the existing Hugging Face discovery and download path. The tasks are:
- A **Classifier** destination type that puts the repository under `classifier/` instead of the default place root.
- A **Classifier** filter on Hugging Face search (tags `decision-model`, `system-one`, `typed-decisions`, `jev`; `pipeline_tag=text-classification`).
- A "ready for llama.cpp" badge when a repository has a GGUF whose header shows decision metadata. The header is read with a range request before download.
- A one‑click **starter set** action on the Classifier page's empty state that queues the six files above to a chosen node.

## 4. Work packages

### WP1 — Files, recognition and the Classifier kind
1. [engines/llama_cpp/gguf.py](../engines/llama_cpp/gguf.py) `facts`: read `<arch>.decision.type`,
   `decision.temperature.*` and `decision.max_head_tokens`, and the presence of the `systemone`
   template. A decision GGUF gets `what: "classifier"` and `decision_type`, plus `vision` when
   an mmproj is paired.
2. [loadcoach/files.py](../loadcoach/files.py) / [file_index.py](../loadcoach/file_index.py):
   `classifier/` is a recognised folder with nested repository folders (depth ≤ 2 for GGUFs,
   ≤ 6 for conversion source bundles). [mirrorwall/assets.py](../mirrorwall/assets.py) `supported`
   accepts safetensors and tokenizer/config JSON under `classifier/` but never `.py` files. A
   source bundle with an `auto_map` or custom code is marked **refused (remote code)**.
3. [weightroom/app.py](../weightroom/app.py) `file_types` gets `("classifier", "Classifier")`. The
   Files pill is rendered by the shared `model_type_choices`. A Files action moves a decision
   GGUF into `classifier/` with the existing arrange/transfer flow.
4. Model setup: adding a classifier uses engine `llama_cpp`. The model form hides chat‑only
   controls (sampling, tools, thinking, chat template override) and shows the decision type,
   choice limit, ubatch requirement (laya/clef) and optional mmproj.
5. Catalogue: `capabilities.classifier = decision_type`, with `chat`, `embeddings` and
   `raw_completion` false. Chat, Knowledge‑answer, Roles and voice pickers exclude classifiers,
   which currently filter only on `embedding`/`image_generation`/`audio`.

### WP2 — Optional conversion job ("Convert to classifier GGUF")
1. A LoadCoach task on one chosen node runs llama.cpp's own `convert_hf_to_gguf.py` (with its
   `conversion/` decision converters) in a **tools venv** (numpy, safetensors, PyTorch CPU).
   The venv is installed on demand through `owg install --classifier-tools install`; its
   llama.cpp source matches the installed build.
2. Routes:
   - native converter for kev LoRA checkpoints, lev, laya, Julia, clef and pplx‑decider sources;
   - **TinyJev pointer adapter** (our code, next to the job): Qwen3 + `head.safetensors` → kev
     GGUF;
   - **metadata patch**: an existing GGUF gets decision metadata and a systemone template
     (TinyJev‑4B v2).
3. Output goes to `classifier/<repo>-decision/` with a manifest: source hashes, converter
   commit, decision type and parity result. Retries use a unique sibling suffix, preserving
   earlier outputs. Sources stay untouched, and the job never runs repository code.
4. **Parity gate:** a fixed question set (choice/score/noul, 30 items, including text, JSON,
   chat, Unicode and HTML inputs) is run through the
   reference (the `tinyjev` package on CPU, or Ollama for TinyJev‑4B) and through llama‑server.
   The job marks the result *verified* when argmax agreement is ≥ 97% and the mean |Δp| is
   ≤ 0.03. Otherwise the result is kept but labelled *unverified*.
5. UI: a Files page action on a source bundle, with progress through the existing task feed.

### WP3 — Serving and endpoints
1. [engines/llama_cpp/adapter.py](../engines/llama_cpp/adapter.py) launch: for classifiers, no
   `--jinja`/chat options, `--ubatch-size ≥ --batch-size` sized to the model's prompt budget
   (required for laya and clef), `--parallel` for concurrent questions, and an optional
   `--mmproj`. A health probe checks `/v1/models` for `output_modalities ⊇ ["decisions"]`.
   A node whose llama.cpp build lacks `/v1/systemone` reports why in Components.
2. Turnstile → LoadCoach routing, with existing auth, model access, allowances, request logs,
   peer routing and busy‑node overflow:

| Endpoint | Behaviour |
|---|---|
| `POST /v1/systemone`, `POST /v1/classifier` (alias) | Native contract. Control node validation: `state` or chat `messages`, 1–64 questions, type‑specific criteria limits, the per‑model choice limit, 64 KiB body cap. Passed through to llama‑server; 400/501 mapped to OpenAI‑style error bodies. Usage counts input tokens only. |
| `GET /v1/models` | Classifier models with `type: "classifier"`, `capabilities.classifier`, `architecture.output_modalities: ["decisions"]` and their limits. |
| `POST /v1/chat/completions` (shim) | When the target is a classifier: `response_format.json_schema` properties map to questions (`enum` → choice, `boolean` → noul, integer `minimum..maximum` or `x-levels` → score; `description` → instructions). The conversation becomes the state. The reply's `message.content` is the JSON answer object, `logprobs.content[].top_logprobs` carries each distribution, `finish_reason: "stop"`, and `completion_tokens: 0`. `stream: true` returns one chunk and `[DONE]`. Any other request returns a 400 that points to `/v1/systemone`. |

3. Cloud APIs ([loadcoach/outside.py](../loadcoach/outside.py), [outside_parts.html](../weightroom/pages/outside_parts.html)):
   each mapped model gets a `systemone` flag. The control node sends `/v1/systemone` to a provider
   such as `https://api.typesafe.ai/v1` or Featherless. The same validation, pricing (input
   tokens), daily ceiling and local‑first fallthrough apply. Credentials stay hidden as they
   are today.

### WP4 — Classifier page (`/chat/classifier`)
A single live design. It reuses frame/parts macros, `model_type_choices`, `node_filter`,
`kpi_parts`, pills and the shared confirmation and empty states.
- **Settings column:** model and node (local and cloud classifiers the caller may use), the
  model's decision type and limits, and an image attachment when the model has vision.
- **Composer:** Text, Chat or JSON state with a token estimate. A **question builder** adds,
  removes and reorders questions. Each has a type pill (Choice · Score · Yes/No), an ID and
  instructions, plus criteria rows (label + description, ordered levels, or true/false
  descriptions). Starters cover support routing, sentiment, spam, urgency 1–5, PII present,
  groundedness and prompt injection. Drafts are kept per model in page memory.
- **Results:** one card per question. Choice shows ranked probability bars with the winner
  marked. Score shows a rubric strip with the expected position and the distribution. Yes/No
  shows a probability meter. A KPI row shows latency, input tokens and the model. An
  uncalibrated‑probabilities note is included.
- **Compare:** the same request on up to 3 models side by side, with disagreements highlighted.
- **Developer drawer:** raw request/response JSON; copy as curl, Python `requests` and the
  OpenAI SDK (shim form).
- **History:** the runs in this tab, with JSON/CSV export.
- **States:** no classifier installed (with the "download starter set" action), model loading,
  over‑limit 400, unsupported 501, provider error, and service disabled.
- Files: `weightroom/pages/classifier.html`, `classifier_parts.html`
  (question/criteria/result DOM templates), `weightroom/static/classifier.css`, `classifier.js`;
  Interact link "Classifier" in [navigation.py](../weightroom/navigation.py).

### WP5 — Classifier pill on every model‑kind surface
`('classifier', 'Classifier')` is added to every `model_type_choices` caller, with correct
behaviour on each: Models catalogue and add form, Files, Dry fit (weights + KV for
`ubatch`/`parallel`), Node measurements, Status and Telemetry model pills, Usage (input tokens),
AI logs (questions, answers and probabilities captured under the existing capture rules), and the
Fingerprints facts.

### WP6 — Benchmarks
1. **Decisions/sec** (Node measurements): load time plus throughput on a fixed request mix
   (1, 8 and 32 questions; 2/8/50 options), reporting decisions/sec and p50/p95 latency per node.
2. **Accuracy suite** (Results, a separate "Classifier" family): labelled decisions scored for
   accuracy, macro‑F1, Brier score and ECE with reliability bins. The data source is the
   pinned `jaredpalmer/kev-suites` held-out split. The repository’s code is Apache-2.0; its
   dataset has no blanket licence and retains the original sources’ terms. It is fetched at
   runtime, hash-checked and never redistributed. Provenance and hashes are recorded in
   `benchmarks/suites/` like the existing suites. Saved scores stay authoritative.

### WP7 — MCP classify tool and Platform services
1. ToolLocker gets a `classify` tool: model, state and questions in, and answers out. It is
   gated by the caller's model access.
2. [platform_services.py](../loadcoach/platform_services.py) gets `allow_classifiers`, which
   covers `kind_enabled('classifier')`, `/chat/classifier`, `/v1/systemone`, `/v1/classifier`,
   the shim, and the MCP tool. It appears as a switch in General settings → Platform services.

### WP8 — Docs, index and tests
- AGENTS.md template index (Classifier page row), `system_docs.py`, `docs/installation.md`
  (llama.cpp build requirement, tools venv), `app-walkthrough.md`.
- Tests:
  - GGUF decision facts, using fixture headers built with gguf‑py;
  - `classifier/` discovery and the remote‑code refusal;
  - control node validation limits;
  - the shim mapping both ways;
  - Cloud API systemone routing and fallthrough;
  - platform gating and model access;
  - pills on all surfaces;
  - the `test_ui_consistency.py` audit;
  - browser checks of the Classifier page (narrow layout, every state);
  - conversion job manifest and parity gate, with stubbed tools.
- End‑to‑end on the GPU node: all six downloaded models through the control node's `/v1/systemone`,
  the shim and the page; both TinyJev conversions through the parity gate.

## 5. Delivery order

| Step | Contents | Done when |
|---|---|---|
| 1 | WP1 + WP3.1–3.2 native endpoint and `/v1/models` | Julia‑1 placed by hand in `classifier/` answers `/v1/systemone` through the control node |
| 2 | §3 downloads (Classifier destination, filter, starter set) | All six models downloaded to the GPU node from the UI and answering |
| 3 | WP4 page + WP5 pills | Interactive page working with all six models |
| 4 | Chat‑completions shim, WP7 | OpenAI SDK and MCP agents can classify; switch disables everything |
| 5 | WP6 benchmarks | Decisions/sec and accuracy results for the six models |
| 6 | WP2 conversion | TinyJev‑0.6B and ‑4B v2 converted and *verified* by the parity gate |
| 7 | WP3.3 Cloud APIs | A TypeSafe/Featherless account answers through the same page and endpoint |

The owner authorized deployment for this delivery. Use `owg update; owg restart`, followed by
a check of the live service source and behaviour on each node that serves classifiers.

## 6. Remaining risks and checks
- **llama.cpp version.** Every classifier node needs a build with `server-decision.cpp`. The
  configured local build is b12008; reports put the merge at about b11361. Components reports nodes that
  are too old.
- **TinyJev‑0.6B on Qwen3.** The adapter passes the expanded native/reference parity gate.
  Future conversions must pass their own gate; failed outputs remain *unverified*.
- **Cloud provider access.** Provider contracts were checked against their documentation;
  live hosted acceptance still requires a configured TypeSafe or Featherless provider key.
- **kev-suites licence.** No dataset licence is declared; fetch the pinned held-out split at runtime and retain upstream terms. Do not vendor the data.

## Using Classifier

Open **Interact → Classifier**, choose a permitted model and an optional compute node, enter
Text, Chat messages or JSON state, and add Choice, Score or Yes/No questions. Score levels
are ordered lowest first; native scores are expected zero-based indices and can be fractional.
Compare adds up to two other models. Drafts and run history stay in page memory; export JSON
or CSV before closing the tab. Probabilities are not calibrated for your data.

Install classifiers queues the six starter downloads into the existing control node Classifier storage.
Use **Install downloaded classifiers** after the files finish; normal installation verifies and
copies them into the chosen GPU node’s active cache. This uses the established central storage
and transfer route without changing node storage settings. Models and Files also expose a Classifier filter.

Use authenticated `POST /v1/systemone` or `/v1/classifier` on the normal model API port.
The JSON Schema chat shim accepts string enums, booleans and integers with 2–10 bounded
levels; it returns a schema-valid integer by rounding the expected level to its nearest index.
Distributions are retained in `logprobs.content[].top_logprobs`, keyed by `question_id`. Native
responses count input tokens only. General settings → Platform services → Classifiers disables
the page, native APIs, shim and MCP invocation.

Cloud APIs mappings offer a **Classifier** model type for providers implementing System One.
Provider keys stay private. Existing model/group access, hosted spending ceilings, sensitivity
restrictions and explicit local-first offload policy apply. Provider contracts were checked
against [TypeSafe](https://api.typesafe.ai/redoc) and
[Featherless](https://featherless.ai/docs/api-reference-classifier).

## Delivery verification

- Deployed through `owg update; owg restart` on the control node and the GPU node; eight classifiers installed on `jpk-main` (six starters plus two verified conversions).
- All six starters: native endpoint, alias, normal/streamed shim and direct app classification return 200; mobile layout has no overflow or browser errors.
- Actual OpenAI SDK: normal response and one streamed chunk, zero completion tokens; MCP classification succeeds on port 8443.
- Restricted test account: empty model catalogue, native/alias 404 and MCP refusal; account/group removed afterwards.
- Platform switch: page/native/alias/shim 403, catalogue hides classifiers and MCP refuses; original enabled setting restored.
- TinyJev-0.6B: expanded parity 30/30 argmax agreement, mean absolute probability difference 0.000494; verified manifest and source hashes retained on the GPU node.
- TinyJev-4B: expanded parity 30/30 argmax agreement, mean absolute probability difference 0.011336; all 426 tensor byte hashes match the source. Both installed model configurations select these verified outputs; native JSON/chat and direct app JSON requests return 200.
- Parity case SHA-256: `f54aca92149a5c02efbfc60ea704dafd4d58fb0e393d998cef9a5f5ac1ebd2f9` (retained in both manifests). Earlier conversion attempts remain labelled with their own outcomes.
- Focused regression suite: 166 pass, including the 39-check UI audit and three browser cases; native routing, cloud stand-in/local-first/access/accounting, conversion receipts, benchmark paths and shared UI surfaces are covered.
- All twelve final benchmark runs completed with current fingerprints: throughput has 27 samples per model; accuracy has 588 cases / 852 questions and ten saved calibration bins per model. Expanded option lists exposed a Kev batch/microbatch clamp; accuracy retries exposed an ambiguous PostgreSQL counter update. Both are fixed and verified live.
- Live TypeSafe/Featherless provider request: pending a configured provider key. Existing Cloud APIs account has no key or mapped models.

Saved results on `jpk-main` (suite-defined microbenchmark; probabilities remain uncalibrated):

| Model | Decisions/s | Accuracy % | Macro-F1 % | Brier | ECE |
|---|---:|---:|---:|---:|---:|
| `julia-1-q8_0` | 104.58 | 46.60 | 26.37 | 0.8512 | 0.3589 |
| `laya-q8_0` | 68.72 | 67.37 | 39.51 | 0.4892 | 0.1483 |
| `kev-0.8b-q8_0` | 30.22 | 81.57 | 69.68 | 0.2707 | 0.0261 |
| `kev-4b-q4_k_m` | 21.29 | 84.39 | 76.13 | 0.2354 | 0.0407 |
| `lev-q4_k_m` | 15.12 | 80.40 | 74.78 | 0.3179 | 0.0965 |
| `kev-9b-q4_k_m` | 18.27 | 86.03 | 74.14 | 0.2119 | 0.0303 |

Optional conversion: install **Classifier conversion tools** on the owning compute node with
`owg install --classifier-tools install` (also offered in the installer's component choices).
**System → Components** reports readiness. Download source data with the Hugging Face
Classifier filter, copy
the complete bundle to that node through Files, open a source file, and choose **Convert to
classifier GGUF**. Tools use the configured llama-server’s exact source commit in a separate
CPU venv. Files shows durable progress and parity; the sibling output includes source/output
hashes, converter commit and manifest. Unverified outputs retain the reason and are never
reported as verified. TinyJev-4B parity installs a digest-verified official Ollama 0.35.1 CPU reference
on demand under the tools directory and runs it privately on a temporary loopback port.
It does not register a service or change an existing Ollama installation.
