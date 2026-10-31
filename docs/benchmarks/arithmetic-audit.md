# Arithmetic score audit — Part D

Completed **2026-10-14**, before Part B, at the owner's request. This was a read-only review
of front's retained benchmark data, not a new inference run. All 60 raw answers, rendered
prompts, extracted answers and field checks were inspected. Current extraction/grading
reproduces every saved field count. No production results, model settings or graders changed.

| Model | Run | Correct fields | Accuracy | Capped cases | Finding |
|---|---|---|---:|---:|---|
| Gemma 31B | [Saved run](http://10.77.10.42:8440/benchmarks/run/batch_0ab9e6a3b9a899875a2d65c7) | 3 / 60 | 5.0% | 19 / 20 | Repeated separators or prompt echoes consume the output budget; the one usable answer is correctly credited. |
| Ministral 14B | [Saved run](http://10.77.10.42:8440/benchmarks/run/batch_fc7d04ff158a7ebc595b6ba5) | 4 / 60 | 6.7% | 18 / 20 | Backtick/brace loops, wrong totals and two uncapped wrong calendar answers. |
| Llama 8B CBT | [Saved run](http://10.77.10.42:8440/benchmarks/run/batch_ccc1d0fbbf320de2cf64ec3c) | 0 / 60 | 0.0% | 16 / 20 | Therapy-domain answers, prompt echoes and repeated prose; no requested JSON answer. Two prose summaries contain correct arithmetic, but fail the response contract. |

All three runs used `arithmetic-v2`, protocol version 2, grader version 2 and a **4,096-token**
answer cap. The present shared allowance is 8,192. These scores describe success on the
recorded request/serving configuration, not a clean ranking of numerical ability. In
particular, extending the answer budget cannot be assumed to fix a delimiter loop.

## Failed samples inspected

The examples below are representative of the full review. Sample names are retained IDs;
expected values come from each run's own saved case, not the later Part B generator.

| Model | Sample | Raw answer and extraction | Classification |
|---|---|---|---|
| Gemma | `integer-1` | Repeated `---` lines; no JSON fields; cap reached. | Generation loop/output cap; no missing grader credit. |
| Gemma | `fraction-1` | Repeated `---` lines; no JSON fields; cap reached. | Generation loop/output cap. |
| Gemma | `list-1` | Repeated `---` lines; no JSON fields; cap reached. | Generation loop/output cap. |
| Gemma | `fraction-2` | Repeats the question and JSON instruction; ends mid-instruction. | Prompt echo/output cap. |
| Gemma | `calendar-1` | Repeated `---` lines; no JSON fields; cap reached. | Generation loop/output cap. |
| Ministral | `integer-1` | Only repeated backticks; no JSON fields; cap reached. | Generation loop/output cap. |
| Ministral | `fraction-1` | Only repeated backticks; no JSON fields; cap reached. | Generation loop/output cap. |
| Ministral | `group-1` | JSON says blue=17, red=8, winner=blue, count=4; expected red=18 and winner=red. | Wrong arithmetic; count correctly receives one field, despite trailing backticks. |
| Ministral | `group-3` | Uses `team` and `amount` instead of requested `totals`; wrong winner; count=4. | Wrong structure and winner; count correctly receives one field. |
| Ministral | `calendar-2` | Says days=20280303 and minutes=477; expected 11 and 257. | Genuine wrong values, uncapped; extra closing brace already tolerated. |
| Llama CBT | `integer-1` | Introduces itself as a CBT therapist; no requested object. | Domain/response-contract failure, uncapped. |
| Llama CBT | `list-1` | Therapy dialogue instead of list transformation. | Domain/response-contract failure, uncapped. |
| Llama CBT | `fraction-1` | Repeated therapy text consumes the output budget. | Domain response plus output cap. |
| Llama CBT | `group-1` | Repeats the request and role markers, without a final object. | Prompt/role echo plus output cap. |
| Llama CBT | `group-2` | Prose says blue=18, red=19, winner=red, count=4, then repeats it; no JSON. | Correct sums expressed outside the requested response contract; not arithmetic inability. |

Gemma's `group-1` is the useful control: it emits `<|channel>thought` / `<channel|>` reasoning
and then the correct JSON, receiving 3/3. Its reasoning marks are already handled.
Ministral's `calendar-4` is also uncapped and genuinely wrong (20280303 and 617 instead of
9 and 377). No valid, correctly keyed JSON object was overlooked in any of the 60 answers.

## Decision and evidence

- **No grader correction is justified.** Preserve the scores and `grader_version: 2`.
  Extracting numbers from arbitrary therapy prose or reasoning would award a different task
  and risks treating intermediate calculations as the answer.
- **Rerun after submission under the intended serving configuration.** Keep native templates,
  current answer allowance, token counts, cap flags and raw output visible. The review does
  not establish whether loops came from model/template compatibility or another generation
  condition; that needs a new controlled run. Do not silently tune serving flags for a score.
- **Part B follows with flat, explicitly named output fields and explicit units.** Use varied
  multi-step questions, exact Python keys and an explicit stopping instruction. Avoid adding a
  grammar that would hide response-contract failures.
- Nine complete controlled-output fixtures, including loops, wrong arithmetic, prose and the
  successful reasoning-mark control, are retained in
  [the regression fixture](../../tests/fixtures/arithmetic_audit_20261014.json). Their raw-output
  SHA-256 hashes tie the tests to the inspected answers.

This closes Part D's evidence audit. Part B's model-quality qualification is separate; no GPU
availability, numerical ranking improvement or production deployment is implied.
