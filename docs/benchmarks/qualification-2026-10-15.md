# Retained benchmark qualification audit — 2026-10-15

**Current status, 2026-10-18: all approved benchmark gates are qualified.** Earlier partial and unqualified results below retain their historical meaning; the [final acceptance audit](../../data/qualification/gates-2026-10-16/final-cleared-gate-acceptance-audit-20261018.json) records current gate acceptance.

The owner reported that monitor use no longer interrupts benchmarks and that the new suites
had run. The initial audit inspected the control node's actual `weightroom/data/benchmarks.db` through a
read-only SQLite connection and one consistent read transaction. No `Benchmarks` store was
constructed against production: its constructor can migrate, regrade and requeue work.
No production row, score or model setting was changed; no inference, deployment or service
restart was performed. All dates below use America/Vancouver.

## Completed run verification

At the initial 2026-10-15 audit, all five completed quality runs used `qwen3.6-35b-a3b-q4`.
The run IDs link to their retained evidence.

| Suite / milestone | Saved run | Samples / checks | Correct checks | Whole cases passed | Actual input tokens |
|---|---|---:|---:|---:|---:|
| Abstention / 98 | [batch_fba92bdcd389777b0c65e94d](http://10.77.10.42:8440/benchmarks/run/batch_fba92bdcd389777b0c65e94d) | 20 / 120 | 100.00% | 20 / 20 | 583–1721 |
| Table reasoning / 99 | [batch_40a883627dc305ee34f59846](http://10.77.10.42:8440/benchmarks/run/batch_40a883627dc305ee34f59846) | 40 / 120 | 10.00% | 0 / 40 | 700–6179 |
| Multi-turn / 102 | [batch_bce291596fb2c2098d2a7474](http://10.77.10.42:8440/benchmarks/run/batch_bce291596fb2c2098d2a7474) | 40 / 120 | 100.00% | 40 / 40 | 357–667 |
| Reasoning / 108 | [batch_e9477e2ae91dd9cc64eafbbb](http://10.77.10.42:8440/benchmarks/run/batch_e9477e2ae91dd9cc64eafbbb) | 40 / 120 | 33.33% | 4 / 40 | 261–410 |
| Instruction hierarchy / 109 | [batch_c7267c15140041308b72e506](http://10.77.10.42:8440/benchmarks/run/batch_c7267c15140041308b72e506) | 50 / 150 | 99.33% | 49 / 50 | 250–663 |

The runs completed on 2026-10-15 between 11:27 and 12:27 PDT. All 190 samples and 630 checks
were audited. Each plan's case IDs exactly match its retained samples. Regenerating cases at
the recorded context reproduces the entire frozen plan. Plan signatures, protocol versions
and implementation hashes matched source at that audit. Every final answer was extracted again from
raw output and retained prompt, and every metric and individual check was recomputed with
the maintained grader. Independent test oracles also solved all 130 actual retained native
conversation cases from their printed puzzle/history/system text; every expected key matched.
There were zero extraction, case, signature or grading mismatches. All 190 probes succeeded; none was unsupported or output-capped.

The three newly reviewed native conversation suites contribute 130 samples and 390 checks.
All 130 have native dialogue and usage receipts. Multi-turn covers all four categories and
both system/user origins; hierarchy covers all five conflict sources, including ten supplied
tool-result histories. Hierarchy's useful-task accuracy is 99% and system-rule accuracy is
100%. Reasoning has four all-correct puzzles, with category accuracy 33.33% ordering,
46.67% scheduling, 53.33% constraints and 0% state transitions. No native reasoning-token
count was reported; it remains unavailable rather than estimated.

Recorded context capacity was 262,114 tokens, but these histories were unpadded: their
actual inputs are the much shorter ranges above. These runs do not qualify 262k-token
answer quality. No yield/resume event occurs in the five completed runs; older cancelled
attempts contain no completed samples. Live pause/resume is therefore not established by
this evidence. Existing local replay/cancellation tests remain separate evidence.

Control node's deployed benchmark registry, worker, shared quality/tool helpers and arithmetic,
retrieval, paired JSON, expanded tool-use, reasoning, hierarchy and multi-turn source files
all match the checkout by SHA-256. Current-version successful runs establish functional
control node/compute execution for the three native conversation suites; file parity alone does
not establish execution of every other new suite.

## Cleared work and remaining gates

- Clear the desktop/monitor interruption correction on the owner's explicit live confirmation.
  The completed reviewed runs have no outside-GPU yield messages. Local model-process,
  admission and watcher regression checks still require competing models to block probes.
- Clear repeated delivery and single-model execution checks for milestones 102, 108 and 109.
  Keep their original four-model score/difficulty calibration, additional template/unsupported
  coverage and live interruption/resume as one pending progression item. Only one model has
  current-version results; multi-turn's 100% score also needs the original ceiling/difficulty
  review before claiming the intended model separation.
- Abstention 98 and Table reasoning 99 remain owner-accepted and already cleared. Their
  retained grades reproduce exactly; Table reasoning's 10% is the measured model result.
- General conversation probe 101 has native text-history and supplied-tool-history evidence
  through these runs. Native constrained JSON and rejection/unsupported-template qualification
  remain with its pending scope and paired JSON milestone 103.

| Other pending increment | Evidence found | Remaining qualification |
|---|---|---|
| Arithmetic B / 80 | Completed arithmetic-v1/v2 only | Run arithmetic-v3 (protocol 3, forty cases) on the prescribed model range |
| Retrieval C / 86 | Completed retrieval-v1 under protocols 1/2 only | Run retrieval-v2 (protocol 3) at its planned lengths on the prescribed model range |
| Paired JSON / 103 | Completed free-only protocols 1/2; no protocol 3 runs | Run thirty paired free/forced cases with native grammar and unsupported coverage |
| Expanded tool use / 104 | Four completed tool-use-v1 runs, twelve cases each; no protocol 2 runs | Run tool-use-v2's sixty-case coverage; preserve accepted P4 v1 results |
| Embedders / 87 | Qwen3 embedding 0.6B context search completed seven probes, maximum successful input 128 tokens; 4B failed before samples because pooling `none` is not OpenAI compatible | Validate model pooling/batch configuration and complete the dedicated context/load/speed checks; no load/speed runs were stored |
| Reusable custom cases / 94 | No `custom_` benchmark runs in the store inventory | Run an imported case set and qualify baseline/persistence/resume |

These rows remain outstanding; older-version completion cannot qualify a changed dataset or
new grammar/tool protocol. Generated-code execution (Part 9) remains owner-deferred.

## Local verification during this audit

- 16 reasoning, hierarchy, multi-turn and documentation checks passed, including independent
  printed-data oracles, unsupported-tool handling and UI coverage.
- 16 selected process-classification, desktop-allocation and watcher checks passed; ten
  unrelated task checks were deselected. These use fixtures, not production GPU inference.
- Both runs report the existing Starlette/httpx deprecation warning.

The private temporary snapshot excludes task tokens and credentials. This report records
aggregate evidence and public benchmark run IDs only; raw retained answers remain in the
existing store. No production cleanup or regrading was performed.

## Four-model follow-up — completed run and retained-record audit

The owner subsequently requested runs on three additional models. Nine runs were submitted
through the ordinary signed-in benchmark queue: Multi-turn, Reasoning and Instruction
hierarchy on `granite-8b`, `qwen-2.5-1.5b` and `smollm2-135m`. The existing Qwen3.6 runs
remain the fourth-model baseline. These are real GPU inference runs, separate from the
initial read-only audit above. No model flags, templates or output limits were changed.

All twelve runs completed. Control node subsequently deployed shared grader 3 and regraded the
retained answers; the live stored checks now match an independent offline recomputation from
native assistant content. A consistent export of the twelve saved runs passed frozen-plan,
source-stamp, answer-key, receipt, hash and score checks: 520 samples, 1,530 possible checks.
Ten SmolLM2 tool-result hierarchy samples are marked unsupported and have no numeric grade.

| Model | Saved Multi-turn run | Correct checks | Whole cases passed | Output-capped cases |
|---|---|---:|---:|---:|
| SmolLM2 135M | [batch_fd91b4b0abeb73c4af629379](http://10.77.10.42:8440/benchmarks/run/batch_fd91b4b0abeb73c4af629379) | 6 / 120 (5.00%) | 2 / 40 | 2 |
| Qwen2.5 1.5B | [batch_61b07b91461706784f33037a](http://10.77.10.42:8440/benchmarks/run/batch_61b07b91461706784f33037a) | 68 / 120 (56.67%) | 19 / 40 | 0 |
| Granite 8B | [batch_dceb683bf3fb4c4949441e01](http://10.77.10.42:8440/benchmarks/run/batch_dceb683bf3fb4c4949441e01) | 120 / 120 (100.00%) | 40 / 40 | 0 |
| Qwen3.6 35B | [batch_bce291596fb2c2098d2a7474](http://10.77.10.42:8440/benchmarks/run/batch_bce291596fb2c2098d2a7474) | 120 / 120 (100.00%) | 40 / 40 | 0 |

Granite's Multi-turn run was paused after five completed samples and resumed through the
normal UI. Those samples were retained and the run finished all forty cases. This supplies
live pause/resume evidence; it does not claim a compute outage or service-restart test.
Both larger models score 100%, so Multi-turn still fails the original difficulty ceiling
and upper-model separation targets. Completing execution does not clear that remaining work.

| Model | Saved Reasoning run | Correct checks | Whole cases passed | Output-capped cases |
|---|---|---:|---:|---:|
| SmolLM2 135M | [batch_befc52a224e6b716870a945d](http://10.77.10.42:8440/benchmarks/run/batch_befc52a224e6b716870a945d) | 0 / 120 (0.00%) | 0 / 40 | 17 |
| Qwen2.5 1.5B | [batch_d75235aa5c5f8b47c7cca8a9](http://10.77.10.42:8440/benchmarks/run/batch_d75235aa5c5f8b47c7cca8a9) | 11 / 120 (9.17%) | 0 / 40 | 0 |
| Granite 8B | [batch_fb46930d11a505e84af84023](http://10.77.10.42:8440/benchmarks/run/batch_fb46930d11a505e84af84023) | 93 / 120 (77.50%) | 31 / 40 | 9 |
| Qwen3.6 35B | [batch_e9477e2ae91dd9cc64eafbbb](http://10.77.10.42:8440/benchmarks/run/batch_e9477e2ae91dd9cc64eafbbb) | 40 / 120 (33.33%) | 4 / 40 | 0 |

Reasoning's highest observed score is 77.5%, below the target 80–95% range; the model order
is not monotonic. Nine Granite and seventeen SmolLM2 cases hit their output cap. Raw outputs
and caps need inspection before changing difficulty or interpreting capability.

| Model | Saved Instruction-hierarchy run | Correct checks | Whole cases passed | Output-capped | Unsupported |
|---|---|---:|---:|---:|---:|
| SmolLM2 135M | [batch_4b3885d0f2e129bca5e2586a](http://10.77.10.42:8440/benchmarks/run/batch_4b3885d0f2e129bca5e2586a) | 0 / 120 (0.00%) | 0 / 50 | 9 | 10 |
| Qwen2.5 1.5B | [batch_19f513d55ec1921f6a6b30c7](http://10.77.10.42:8440/benchmarks/run/batch_19f513d55ec1921f6a6b30c7) | 7 / 150 (4.67%) | 0 / 50 | 0 | 0 |
| Granite 8B | [batch_23c9bcff63a1013a36e01ce6](http://10.77.10.42:8440/benchmarks/run/batch_23c9bcff63a1013a36e01ce6) | 150 / 150 (100.00%) | 50 / 50 | 0 | 0 |
| Qwen3.6 35B | [batch_c7267c15140041308b72e506](http://10.77.10.42:8440/benchmarks/run/batch_c7267c15140041308b72e506) | 149 / 150 (99.33%) | 49 / 50 | 0 | 0 |

Instruction hierarchy also has a ceiling at the upper end. The ten unsupported tool-result
samples remain outside the scored denominator; they are not incorrect answers. The smaller
models separate, but the two larger ones do not meet the intended upper separation.

Granite exposed a grading defect: native assistant content already held the final JSON,
with reasoning in a separate field, but the old extractor discarded that content because
the serialized prompt ended in `<think>`. Local commit `5827aead` uses server-parsed native
assistant content directly and retains marker extraction for raw completions. Shared quality
grader 3 and imported-case grader 2 repair retained evidence without asking models again.
All 142 focused feature checks and two documentation checks passed, including PostgreSQL.

A consistent online SQLite snapshot was copied privately for startup/regrading verification.
On that copy, 249 terminal runs regraded successfully; a second pass regraded zero runs.
Integrity, table counts, raw sample outputs/logs, native evidence and frozen plans were
preserved. Run messages retained their existing 500-message bound. Correctness changed only
for the forty affected Granite Multi-turn samples; 723 historical JSON samples acquired
the existing paired grader's `pair`/`decoding` metadata without correctness changes.
That copy-only regrading check did not alter the live store. The other session deployed grader
3 while the benchmark queue was finishing. The final audit above verified the resulting live
saved records; no new inference was needed for regrading. The four-model execution and
retained-record checks are complete. Difficulty, output-cap analysis and additional native
template coverage remain pending in Progression.
The large-model baseline here was Qwen3.6 35B, whereas Progression's original example names
Qwen3.8 27B; that exact example model has not been run for these three suites.

## Four-model spread sweep — retained A2 reconciliation, 2026-10-16

A read-only transaction against the control node's current `benchmarks.db` read 38 relevant attempts
(27 completed, 10 failed, one cancelled) and all 884 saved measurement rows. The saved module
summaries and configured formulas give these displayed scores;
paired JSON scores free checks only, and tool use scores strict task completion. No row was changed
or regraded. [Machine-readable receipts](../../data/qualification/g02-retained-a2-2026-10-16.json)
include exact run IDs, protocols, caps, unsupported counts, formulas and the eight resubmissions.
Target: best 80–95, smallest under 30, and the middle two clearly apart.

| Suite | SmolLM2 135M | Qwen2.5 1.5B | Qwen3.5 9B | GPT-OSS 20B | Interpretation |
|---|---:|---:|---:|---:|---|
| `json_schema` v3 | 14.1 | 78.2 | 96.2 | 98.7 | Upper two exceed 95; free-answer headline |
| `abstention` v1 | 0.0 | 26.7 | 90.0 | 85.0 | Upper pair reversed; SmolLM2 capped 20/20 |
| `arithmetic` v3 | 0.0 | 5.5 | 82.0 | 81.3 | Upper pair nearly tied/reversed; SmolLM2 capped 27/40 |
| `tables` v1 | 0.0 | 5.0 | 21.7 | – | GPT-OSS retry failed; Qwen3.5 capped 31/40 |
| `tool_use` v2 | unsupported | 15.0 | 71.7 | 55.0 | Top under 80 and reversed; SmolLM2 60/60 unsupported |
| `retrieval` v3 | 0.0 | 30.0 | 0.0 | 32.5 | Output caps invalidate the comparison |
| `custom_example-checks` v1 | 0.0 | 33.3 | 33.3 | 100.0 | Three imported cases establish execution only |

Seven of eight selected resubmissions completed: SmolLM2 arithmetic; Qwen3.5 JSON, abstention,
arithmetic, tables and imported cases; and GPT-OSS tool use. GPT-OSS tables
`batch_1686c2ab15f176b85bbe9de0` retained 13 samples, then failed because another GPU process
appeared during measurement. It is the sole outstanding resubmission. A later cancelled
Qwen2.5 JSON attempt does not replace its completed v3 result.

Caps materially limit the low and table scores. SmolLM2 capped 38/40 table and 8/8 retrieval
cases. Qwen3.5 capped 2/20 abstention, 4/40 arithmetic, 31/40 tables and 20/20 retrieval;
all those capped cases have empty saved answers. GPT-OSS capped 1/20 abstention, 3/40 arithmetic
and 10/20 retrieval, also with empty saved answers. Its tool run scored 55% completion, with only
39/60 samples reporting successful execution; that score is not its raw check accuracy.
Qwen3.5's two capped imported cases underscore why three examples cannot establish spread.

The retained retrieval-v3 runs remain invalid for spread: Qwen3.5 capped 20/20, GPT-OSS 10/20,
and SmolLM2 8/8; GPT-OSS's ten uncapped cases scored 65%. The newly deployed retrieval-v3
dataset uses protocol 4, paired JSON v4 and extraction-v5 protocol 3; their future runs must be
assessed separately from these frozen older versions. The GPT-OSS table resubmission and D7
representative sampling are reported below. No retired v1 A3 rerun is required.

## Representative qualification paused — 2026-10-16

The owner paused the live queue for other work. Control node `benchmark_state.paused=1`;
the 13 submitted runs contain four completed and nine queued, with none running or failed.
LoadCoach reports no running/waiting work, holds, lock or loaded model. Completed runs are
GPT-OSS tables v1 (40/40 samples, 15.83% score, 33 output caps), Qwen2.5 paired JSON v4
(30/30, 3.85% free-answer score, 0 caps), GPT-OSS paired JSON v4 (30/30, 76.92%
free-answer score, 2 free-answer caps), and Qwen2.5 reasoning v2 (48/48, 14.58% score,
0 caps). The GPT-OSS reasoning run is queued with six completed samples retained; pause
discarded its incomplete sample. Eight later runs have not started. Exact IDs, saved
metrics, and the pause snapshot are in the existing private
[representative run receipt](../../data/qualification/gates-2026-10-16/representative-runs.json).

The temporary `Test benchmarkl` fixture schedule was subsequently deleted; preserve
that deletion rather than restoring it after qualification. Its original state and
current read-only observation are in
[the intake receipt](../../data/qualification/gates-2026-10-16/intake-and-schedules.json).

## Representative qualification resumed — 2026-10-17

The owner requested continuation. All nine pending runs passed live task authorization,
model fingerprint, engine/machine environment and implementation/protocol checks before
WeightRoom Resume all cleared the global pause. GPT-OSS reasoning resumes with its six
saved samples; completed runs stay unchanged.

## Representative results and limits — 2026-10-17

All thirteen retained runs completed with frozen model/protocol/environment identity, finite
scores and no native sample failures. The [private machine receipt](../../data/qualification/gates-2026-10-16/representative-runs.json)
holds exact run IDs, plans, metrics and cap counts. Scores below are correct-field headlines;
JSON scores use only the fifteen free-answer cases per model.

| Retained calibration contract | Qwen2.5 1.5B | GPT-OSS 20B Q4 | Qualification |
|---|---:|---:|---|
| Tables v1 | earlier 5.0% | 15.83%, 33/40 capped | Large-table ability unqualified; all 80/200-row GPT-OSS cases capped. |
| Paired JSON v4 | 3.85%, 0/15 free capped | 76.92%, 2/15 free capped | Free/forced modes separate; GPT-OSS uncapped free subset is 60/70 checks (85.71%). |
| Reasoning v2 / protocol 2 (historical) | 14.58%, 0/48 capped | 63.19%, 10/48 capped | Below the 80% target; uncapped subset is 91/114 (79.82%). Per-cap looping/still-reasoning classification remains unverified. |
| Hierarchy v2 | 6.70%, 0/56 capped | 88.39%, 4/56 capped | Useful representative separation; caps below 10%, no unsupported cases. |
| Turns v2 | 40.10%, 0/48 capped | 58.33%, 0/48 capped | Saved value errors are real, but the stronger model does not reach the spread target. |
| Extraction v5 / protocol 3 | 18.06%, 0/36 capped | 86.46%, 1/36 capped | Useful separation, but the suite's zero-cap qualification flag is false. |
| Retrieval v3 / protocol 4 | 33.13%, 0/16 capped | 62.00%, 3/20 capped | Strong output caps are 15%; model contexts generate different input-size sets. |

The [private suite cap-policy audit](../../data/qualification/gates-2026-10-16/reasoning-cap-policy-review.json) applies the approved A3 still-reasoning rule to both models and reports A2 table/tool caps without an invented ceiling; 27 focused checks and completed hierarchy/extraction regressions pass. Required classifications bind to the complete native message; unknown causes remain unknown.

Hierarchy-v2 clears its D7 representative gate: the paired scores meet the approved sampled
separation and cap limit. This does not claim a full four-model ranking; milestones 109/120
are recorded complete and removed from the remaining G03 scope. Its
[retained-record audit](../../data/qualification/gates-2026-10-16/hierarchy-retained-audit.json)
also reproduces both complete plans, native final answers, individual checks and summaries
without changing the store.

Retrieval's common 1,024/4,096/8,192-token sizes give Qwen2.5 44/120 checks (36.67%)
and GPT-OSS 86/120 (71.67%), with two strong caps. Qwen2.5 also has four 16,384-token
cases; GPT-OSS instead has four 32,768- and four 65,536-token cases. Its eight larger cases
score 38/80 checks (47.5%). Compare common sizes before reading the full scores as a spread.

The capped GPT-OSS table and JSON outputs visibly repeat calculations or short deliberation
fragments until their limits. Increasing retrieval's answer room to about 32,768 tokens did
not prevent three strong caps, including repeated arithmetic. A scoped tables-v2 candidate
(20/30/40 rows and a direct-JSON request) was stopped after 14/40 saved samples when seven
had capped; control node and local code were restored to tables-v1. A third-model turns-v2 diagnostic
with Qwen3.5 9B Q8 was stopped after 20/48 saved samples by the then-used five-total-cap guard; its fifteen uncapped cases scored 60/60 checks. That operational stop does not classify the caps under A3's approved still-reasoning rule. Both cancelled
diagnostics retain their samples and private source/identity receipts. They are not full-run
scores. Further fixture easing or answer-room increases cannot be counted as a correction
without an explicitly versioned contract and new representative qualification.
Read-only regrading of all 546 successful saved samples across these fifteen runs reproduced
every stored correct/check/passed field; the observed limits are not a saved-score drift.
The temporary fixture schedule was later observed deleted and disabled in the control node's schedule
store; preserve that later state rather than re-enable it from the earlier intake snapshot.

Expanded tool-use-v2 initially remained open with Qwen3.5 at 71.7%; its later protocol-3 qualification is recorded below. Accepted tool-use-v1 delivery is unchanged. Arithmetic-v3 and
abstention-v1 already meet the retained score-range target; the [cleared-section evidence inventory](../../data/qualification/gates-2026-10-16/cleared-benchmark-completion-inventory-20261018.json) rechecks their current protocols, native coverage and grades alongside paired JSON, hierarchy and extraction. The three imported custom cases
qualify execution only. D7 reduces mandatory model coverage without waiving score targets.

An isolated native-chat sampler comparison reproduced the original capped 80-row table case
`filter-2` with GPT-OSS at context 65,536, output limit 8,192 and seed 42. Both requests used
top-p 1, top-k 0, min-p 0 and no repetition penalties. Temperature 0 exhausted 8,192 tokens
without a final answer (0/3 checks); temperature 1 finished naturally at 3,511 tokens with
bare JSON and 3/3 checks. This establishes a sampler effect for one matched case, not full
suite qualification. [Private request/response identities and measurements](../../data/qualification/gates-2026-10-16/sampler-diagnostic.json)
are retained. The temporary native process, lock and key were cleaned up; normal model
settings and old benchmark records were unchanged.

The original 40-case, 30/80/200-row `tables-v1` dataset is retained in sampler protocol v4;
its frozen case/question/key hash is `b42b94d9a547aa37b6f06e8ba36fe0ae7499f65dc3299784f43130a5a9f3a06b`.
Protocol v4 freezes temperature 1, top-p 1, top-k 0, min-p 0, disabled repetition penalties,
generation seed 42, and at most 32,768 output tokens. The `seed` 20261015 field denotes fixture
generation. The preceding protocol-v3 run `batch_178f40eae30b54a5f1e96a0a` was cancelled
after one retained uncapped 3/3 sample because its stored top-level temperature still said 0;
its evidence remains a diagnostic. Control node protocol v4 was deployed and run
`batch_4d9b05f3122bad8bbe94ec48` submitted on 2026-10-17 with matching stored metadata.
That GPT-OSS attempt was stopped after 27/40 cases with 54/81 checks correct and two
output caps: 27 already-wrong checks bound its best possible full result at 93/120 (77.5%),
below the 80% target. Its 66.67% retained accuracy is a partial diagnostic, not a full-run
score. An uncapped join answer had valid bare JSON but incorrect selected total and order
count; the greatest-order identifier matched. The queue, requests, holds, lock and loaded
model list were empty after stopping. The unchanged table cases were selected for Qwen3.5
before fixture revisions; [private retained diagnostics](../../data/qualification/gates-2026-10-16/representative-runs.json).

Qwen3.5 protocol-v4 run `batch_0923776968301a37e235569b` was stopped after
18/40 cases with 34/54 correct checks (62.96%) and five 32,768-token caps
under the then-used operational cap guard. Its full-plan score bound was still
100/120; the stop was not mathematical score futility or a current A2 cap-count acceptance rule. This is a partial diagnostic. All retained identities, plans, native receipts and grades reproduce;
[record checks](../../data/qualification/gates-2026-10-16/table-v4-retained-audit.json).
A [five-cap/current-plan comparison](../../data/qualification/gates-2026-10-16/table-v4-qwen-cap-current-plan-review.json) finds only `group-2` unchanged; the other four capped requests changed from 200 to 120 rows, so the earlier failure does not rule out a fresh protocol-5 run.
Manual cap-tail inspection found ongoing row counting, sums and self-verification,
including one 30-row case; these are room/self-check limits rather than a scorer
correction. [Diagnostic observations](../../data/qualification/gates-2026-10-16/table-caps-diagnostic.json).
The locally reviewed `tables-v3` / protocol 5 candidate reduces only the thirteen
200-row cases to the first 120 physical rows after all original RNG draws. Independent
baseline comparisons at context limits 32,768 and 131,072 confirm all 27 smaller cases
are identical; a printed-question SQL oracle reproduces all 120 fields. Eleven focused
tests pass; the first-ten-row shortcut scores 10/120 (8.33%), null-only zero. Sampler,
seed, skills, formats and field grader are unchanged. Source SHA-256 is
`b78251184195bd2657fd3bb2e596dc7a34010af355795dedb4ebefa8a9cbdc0c`.
Control node deployment was hash-verified on 2026-10-17 after pausing at four persisted turns
samples; turns source, protocol and frozen plan stayed unchanged and resumed with all four.
An independent retained audit confirms those identities and grades. Fresh table native
qualification remains; no historical sample is reclassified.

A subsequent independence audit found that grader 3's 120 fields include 24 dependent
outputs: group winner/count follow from the returned totals (the count is two in all eight
current cases), and `third_id` repeats the third ranked ID. Only 96 nonredundant graded checks
remain, below the required 100. The reviewed table grader 4 scores four separate group
sums and three ranked slots plus the named position, yielding 136 headline checks; the derived
24 remain auxiliary whole-case checks. All 40 case dictionaries, requests and keys are identical
at three context limits. This is a scoring-only correction: keep protocol 5 / `tables-v3`,
retain original grader-3 receipts and frozen source/plan identities, then regrade terminal saved
answers under grader 4. It was deployed on the control node after the active table run became terminal
on 2026-10-17. Independent before/after comparisons cover 21 terminal runs, 350 samples/evidence
and 14 plans: raw outputs/logs, native evidence, generation identities and frozen plans are
unchanged, and all regraded checks reproduce. [Preservation audit](../../data/qualification/gates-2026-10-16/table-grader4-preservation-audit.json).
Native qualification remains open; earlier scores describe grader 3.
The 78-test table/shared-quality/documentation regression run passes (one existing
Starlette/httpx warning); generator/solver/renderer/worker ASTs are unchanged and independent
review confirms loose-value parity. [Private review receipt](../../data/qualification/gates-2026-10-16/table-grader4-review.json).

GPT-OSS table run `batch_bd7521d536dc37c213476354` stopped at 27/40 cases, with two caps.
Grader 3 scored 60/81 checks (74.07%); grader 4 scores 68/97 independent checks (70.10%).
The 29 already-wrong independent checks bound the best possible full result at 107/136
(78.68%), below 80%; this is an upper bound, not a measured final score. The run remains a
partial diagnostic. [Grader-4 native/plan audit](../../data/qualification/gates-2026-10-16/table-v5-grade4-terminal-audit.json)
accepts only the reviewed scoring change with exact regenerated cases; completion and coverage
correctly remain false. After clarification that 78.68% is only the ceiling, the owner requested
completion of all 40 cases rather than acceptance of the partial result. Normal API-created
continuation `batch_91483b8bdc1c1ec01bd91a5a` reuses those 27 observations with explicit
source-run/sample/protocol/timestamp provenance and executes only the remaining 13 through
the normal worker. Exact 40-case, identity, native-receipt and source-preservation checks passed
before release; 16 offline recovery/auditor tests pass. [Launch receipt](../../data/qualification/gates-2026-10-16/table-v5-continuation-launch.json).
The continuation saved seven more cases before an external GPU-process integrity guard stopped it at 34/40 (78/118 checks, 66.10%, three caps); the interrupted case was discarded. Fresh normal API target `batch_f849405fb90a73ee7b6bc34a` retains the 34 verified observations and measures only `dirty-3` through `dirty-8`. Frozen importer/auditor checks and 19 recovery tests pass; independent live partial audit confirms the unchanged 27+7 source chain and exact remaining six. The target completed all 40 cases: 91/136 independent checks (66.91%), 20/40 whole cases (50%), three caps, zero unavailable/unsupported/native failures. Independent full-plan/native/scoring and 27+7+6 lineage audits pass with unchanged source snapshots. [Full native audit](../../data/qualification/gates-2026-10-16/table-v5-full40-native-audit.json). This completes the owner-requested measurement as a composite of verified observations, not 40 new executions; table qualification remains open below the 80% strong-model floor. Probe fix `55a32aa1` routes future during-measurement GPU interference through the existing three-attempt HTTP 502 retry budget, stopping only its own child; 27 task and five focused regression checks pass. The retry fix was activated on compute at a verified quiet boundary on 2026-10-18; source parity, unchanged private configuration and both GPU cards passed live checks. Controlled interference retry remains fixture-verified; no live interference injection was performed. [Worker delivery](../../data/qualification/gates-2026-10-16/worker-delivery-20261018.json).
A [three-case printed-prompt review](../../data/qualification/gates-2026-10-16/table-v5-failed-output-review.json) reproduced the dirty/group/rank keys and found arithmetic, requested-shape and ranking errors in the model answers; it justifies no scorer or fixture correction.
Expanded tool-use separately has 128 conservative independent scored
units (64 unique expected calls, each with separate tool-name and required-argument checks);
its final-answer and structural/aggregate checks are excluded from that count.
Qwen3.5 9B table run `batch_1d4dfb5afa3b23da60672bf0` was stopped through the supported UI after 10/40 cases: 20/32 checks (62.5%), two caps and zero native failures. Both first group cases exhausted 32,768 tokens without a final answer, including the 30-row case. This [scheduling stop](../../data/qualification/gates-2026-10-16/table-v5-qwen9-scheduling-stop.json) made room for independent parser/reasoning work; the best possible full score was still 124/136 (91.18%), so it is not a mathematical disqualification. The [independent terminal audit](../../data/qualification/gates-2026-10-16/table-v5-qwen-root-terminal-audit.json) passes all applicable identity, plan and grading checks; completion/coverage correctly remain false. Representative table qualification remains open.
An [independent printed-question audit](../../data/qualification/gates-2026-10-16/table-v5-qwen-printed-key-independent-audit.json) reproduces all 40 keys without the benchmark solver or stored source rows/options. The [difficulty analysis](../../data/qualification/gates-2026-10-16/table-v5-gpt-qwen-difficulty-analysis.json) confirms all three earlier GPT caps occurred at 120 rows, with genuine arithmetic errors also present at smaller sizes; it establishes no qualification or guaranteed revision outcome.

The reviewed `tables-v4` / protocol 6 uniformly projects row levels to 30/60/80 after consuming the original draws, preserving fourteen complete cases and all 136 semantic fields. The [bounded revision audit](../../data/qualification/gates-2026-10-16/table-v4-30-60-80-candidate-validation.json) includes 21 isolated regression tests, 120 printed-key SQL oracles and independent prefix/function comparisons. [Control node delivery](../../data/qualification/gates-2026-10-16/tables-v4-front-delivery-20261018.json) on 2026-10-18 verified the one-file source change, unchanged shared quality/Turns/Reasoning sources, live protocol 6 and an empty queue after a WeightRoom-only restart. Qwen2.5 1.5B weak run `batch_be73f358777c2b1d133f4c69` completed 40/40 cases at 0/136 checks, with zero caps or native failures; every [independent full audit](../../data/qualification/gates-2026-10-16/tables-v4-qwen15-root-full-native-audit.json) check passes. Sampled native JSON fields contain wrong numeric values, not lost extraction. GPT-OSS strong run `batch_2e06e1363eee69c9ddd8e620` stopped at the frozen futility boundary: 34/40 cases, 90/118 checks (76.27%), zero caps or native failures; its best possible full-plan score was 108/136 (79.41%), below the 80% floor. The [independent terminal audit](../../data/qualification/gates-2026-10-16/tables-v4-gpt-root-terminal-audit.json) reproduces identity, cases, keys and grades while correctly leaving completion and full coverage false; printed-row recomputation confirms genuine arithmetic and output-shape misses. Strong qualification remains open. No earlier native result is imported or regraded into this revision.

The bounded `tables-v5` / protocol 7 revision uniformly projects every original draw to 20/40/60 rows, preserving the five skills, CSV/Markdown mix, all 136 independent checks and grader 4. All forty displayed tables are exact prefixes of protocol 6; queries and keys are recomputed from those printed rows. The [local validation](../../data/qualification/gates-2026-10-16/table-v5-20-40-60-candidate-validation.json) includes 27 isolated regressions and 160 independent printed-table SQL oracles across four contexts. First-ten-row and null shortcuts score 8.09% and 0%; no answer keys reach inference. [Control node delivery](../../data/qualification/gates-2026-10-16/tables-v5-front-delivery-20261018.json) on 2026-10-18 verified the one-file source change, unchanged shared quality/Turns/Reasoning/app sources, live protocol 7 and an empty queue after the WeightRoom-only restart. Qwen2.5 1.5B weak run `batch_2394325bed7c586bf9c99590` completed all 40 cases at 1/136 checks (0.74%), with zero caps, unsupported cases or native failures; all [independent full audit](../../data/qualification/gates-2026-10-16/tables-v5-qwen15-root-full-native-audit.json) checks pass. GPT-OSS strong run `batch_fdd403ba26fa3b07cfb731c4` stopped at the frozen futility boundary: 36/40 cases, 95/124 checks (76.61%), zero caps or native failures; even perfect remaining answers would yield only 107/136 (78.68%). Its [independent terminal audit](../../data/qualification/gates-2026-10-16/tables-v5-gpt-root-terminal-audit.json) reproduces all record-integrity checks, with completion and full coverage correctly false. Genuine arithmetic and output-shape misses remain; strong qualification is open and earlier runs remain frozen diagnostics.

The bounded `tables-v6` / protocol 8 revision selects 10/20/30 rows with one deterministic structural policy: two rows per group, or a duplicate pair plus both blank types and numeric commas for dirty data, followed by even source positions. It preserves original random draws, source order, all five skills, 40 cases and 136 independent checks; explicit parent-row indices make every selection auditable. Grader 4, sampling and output allowance are unchanged. The [local validation](../../data/qualification/gates-2026-10-16/table-v6-10-20-30-candidate-validation.json) passes 28 isolated regressions and 160 printed-table SQL oracles across four contexts; first-three/five/ten-row shortcuts score 5.88%/7.35%/39.71%, and null scores zero. [Control node delivery](../../data/qualification/gates-2026-10-16/tables-v6-front-delivery-20261018.json) on 2026-10-18 verified exact source/implementation identity, unchanged shared quality/Turns/Reasoning/app sources and an empty queue after a WeightRoom-only restart. Fresh GPT-OSS strong run `batch_dcd5842d4dbfca8cbce3f8c7` and Qwen2.5 1.5B weak run `batch_4fff961a400a89b8c3b250f1` each completed all 40 native cases: strong 126/136 checks (92.65%), 35/40 whole cases; weak 7/136 (5.15%), 0/40 whole cases. Both have zero caps, unsupported cases and native failures. All 24 run checks and all nine comparison checks pass in the [independent paired audit](../../data/qualification/gates-2026-10-16/tables-v6-root-paired-audit-20261018.json): 40 matched cases, 136 independent checks and an 87.5-point spread. The [actual native stack](../../data/qualification/gates-2026-10-16/tables-v6-gpt-root-live-child.json) is verified; [all five wrong strong answers](../../data/qualification/gates-2026-10-16/tables-v6-root-wrong-printed-sql-audit.json) contain genuine selection, arithmetic, deduplication or blank-count errors against independently parsed printed tables. [Final cleanup](../../data/qualification/gates-2026-10-16/tables-v6-final-qualification-20261018.json) confirms an empty queue, no holds/locks/loaded native servers and no remaining guard key. Tables qualification and G02 are cleared under D7; no older samples are imported. This representative comparison does not claim a full-catalogue ranking or long-context quality.

Current tool-use-v2 / protocol 3 Qwen3.5 run `batch_39b8ef2cc79daab0a697bb6e` stopped automatically at 36/60 tasks: 23/36 completed (63.89%), 13 incomplete, zero caps/unsupported/native failures. Even perfect remaining tasks would give 47/60 (78.33%), below the strong floor. The [retained native audit](../../data/qualification/gates-2026-10-16/tool-use-v2-p3-qwen-terminal-audit.json) reproduces identity, plan, keys and grades; completion/coverage correctly remain false. [Failure review](../../data/qualification/gates-2026-10-16/tool-use-v2-p3-qwen-failures.json) identifies bare answers instead of requested JSON, final values stranded in reasoning, and unrelated/repeated calls; no harness extraction defect was found. That diagnostic required another representative strong model; the later qualifying pair is recorded below.
GPT-OSS retry `batch_e8334a3124502e114eac2ed1` stopped at 32/60 tasks: 19 completed (59.375%), 13 incomplete, zero caps. Its best full-plan result is 47/60 (78.33%); the [terminal audit](../../data/qualification/gates-2026-10-16/tool-use-v2-p3-gpt-terminal-audit.json) passes identity/plan/grading checks with completion/coverage false. Twelve incomplete tasks are [native parser compatibility failures](../../data/qualification/gates-2026-10-16/tool-use-gpt-p3-retry-classification.json), including JSON bodies with a Harmony constraint header the ordinary final parser rejects; they remain wrong tasks on their recorded stack, not proof of incorrect JSON content. The earlier ten-case diagnostic was stopped by an overstrict private guard; the reviewed replacement counts these failures and stops other operational failures. The [same-base parser correction](../../data/qualification/gates-2026-10-16/gpt-oss-parser-activation-20261018.json) was activated at a verified idle boundary on 2026-10-18 after parser fixtures and the candidate-library runtime harness passed. The first fresh GPT child passed [executable and mapped-library verification](../../data/qualification/gates-2026-10-16/gpt-oss-parser-live-child-maps-20261018.json); strict JSON grading is unchanged. Fresh Reasoning protocol-3 qualification and current Tool-use evidence are recorded below. Reserve Qwen3.6 35B run `batch_5a52bf35d4d2da763bafb12d` used the same frozen task plan and existing settings, then stopped at 13/60 with 0 completed tasks, zero caps/native failures and a best possible 47/60. Its [terminal audit](../../data/qualification/gates-2026-10-16/tool-use-qwen36-p3-terminal-native-audit.json) reproduces identity, plan and grades; [retained final-field evidence](../../data/qualification/gates-2026-10-16/tool-use-qwen36-p3-terminal-receipt.json) shows prose despite the explicit system JSON instruction. Independent printed/native inspection confirms this for three cases. These earlier diagnostics did not qualify the suite; the corrected-stack pair is recorded below.

First corrected-parser GPT Tool-use run `batch_ddf7c4a8470db9796fac64be` completed all 60 attempts: 48 strict tasks (80.0%), zero caps, 55 successful native observations and five PEG parse failures. The [full record audit](../../data/qualification/gates-2026-10-16/tool-use-gpt-parser-full-native-audit.json) reproduces identity, plan, keys and grades; [failure evidence](../../data/qualification/gates-2026-10-16/tool-use-gpt-parser-failures.json) binds four format-label headers and one malformed multi-channel header to their logs. All five remain wrong task outcomes; no other operational failure occurred. The bounded format-label correction passed [CPU and candidate-library fixtures](../../data/qualification/gates-2026-10-16/gpt-oss-format-header-relink-candidate.json), including byte-wise rejection of the exact malformed multi-channel header without tool routing. It was [activated at idle](../../data/qualification/gates-2026-10-16/gpt-oss-format-header-activation-20261018.json) on 2026-10-18; the fresh GPT Tool-use run `batch_01098168a853881f3962d8e9` passed [actual executable and mapped-library verification](../../data/qualification/gates-2026-10-16/gpt-oss-format-header-live-child-maps-20261018.json) and completed as recorded below. The first 80% baseline remains unchanged.

Tool-use protocol 3 qualified on 2026-10-18: fresh GPT-OSS strong run `batch_01098168a853881f3962d8e9` scored 50/60 strict tasks (83.33%); Qwen2.5 1.5B weak run `batch_aed1cf20a13825c9b56287eb` scored 1/60 (1.67%). Both completed the same 60 cases and 128 independent semantic checks; all 24 run checks and all noncoverage comparison checks pass in the [unchanged paired audit](../../data/qualification/gates-2026-10-16/tool-use-p3-format-root-paired-audit.json). Native-success counts remain 59 strong and 60 weak: strong `near_miss-9` emitted the exact malformed multi-channel header, which stays a wrong task. The [Tool-only supported-execution audit](../../data/qualification/gates-2026-10-16/tool-use-p3-supported-execution-root-audit.json) binds its terminal request, prompt-token receipt, raw output/log/error, bytewise rejection fixture and actual parser stack; it proves full 60/60 supported execution for each model under the existing malformed-answer rule. Generic successful-native-coverage flags remain false; no case is excluded or given credit. Seven adversarial checks reject stale/missing evidence, valid-header parser bugs and operational failures. Both plans have zero reported caps, unsupported cases or operational failures. Milestone 104 qualification is cleared; G02 retains Tables only.

Reasoning protocol 3 / `reasoning-v2` completed fresh GPT-OSS strong run `batch_b6d249dd08a967973a9c9301` and Qwen2.5 1.5B weak run `batch_84eb07717aab1501cc659555` on 2026-10-18. Both completed all 48 native cases with zero caps, unsupported cases or native failures: strong 123/144 checks (85.42%), 40/48 whole cases; weak 22/144 (15.28%), 0/48 whole cases. Every run and comparison check passes in the [independent paired full audit](../../data/qualification/gates-2026-10-16/reasoning-p3-paired-root-full-native-audit.json), including the same 48 semantic cases and 144 independent common checks. The 70.14-point spread meets D7; Reasoning qualification is cleared. Original milestone-101 native rejection acceptance is recorded below.

Extraction protocol 4 GPT-OSS run `batch_293a8f0b78a4bb65b5914049` completed all 36 cases:
262/288 checks (90.97%), 22/36 whole cases (61.11%), zero caps and zero unavailable/failed/
unsupported cases. Frozen identity, plan/signature, exact regeneration, native receipts and
independent regrading all pass. [Native audit](../../data/qualification/gates-2026-10-16/extraction-v4-gpt-native-audit.json).
Qwen2.5 1.5B run `batch_250bb74602e9ad166ead91fd` completed the same 36 cases with
53/288 checks (18.40%), 0/36 whole cases, zero caps and zero unavailable/failed/unsupported cases.
All frozen-plan, identity, native-receipt and independent-grading checks pass for both runs;
all 288 semantic checks match exactly. [Paired native audit](../../data/qualification/gates-2026-10-16/extraction-v4-matched-native-audit.json).
The 72.57-point spread meets D7's strong/weak bands with complete zero-cap coverage;
extraction qualification is cleared. G05 now retains retrieval only.

Retrieval protocol 5 / `retrieval-v3` GPT-OSS run `batch_10b10a3c2f76357f192cc362` stopped at 11/20 cases: 84/110 checks (76.36%), zero caps/native failures. Its 26 wrong checks among the fixed 12 common cases bound the best matched result at 94/120 (78.33%), below 80%; the unmatched larger-input cases cannot repair that comparison. [Retained native audit](../../data/qualification/gates-2026-10-16/retrieval-v5-gpt-terminal-native-audit.json) confirms identity, exact keys and saved grades; completion/coverage correctly remain false. Wrong division, malformed JSON and excluded-archive inventions are model errors, with no prompt/key defect found. Qwen3.5 9B strong alternative `batch_c10eeeb645d4789cc09da184` completed all 20 cases: 166/200 checks (83.0%) overall, 99/120 (82.5%) on the 12 common cases, zero caps and 20 successful native results. All frozen-plan, identity, coverage and retained-grade checks pass in the [full native audit](../../data/qualification/gates-2026-10-16/retrieval-v5-qwen-full-audit.json). Qwen2.5 1.5B weak run `batch_70c5b4842663f38c13360466` completed 16/16 cases: 11/160 checks (6.875%) overall and 11/120 (9.17%) on the same 12 common cases. Whole-case accuracy is 5/20 (25%) strong and 0/16 weak; both full plans have zero caps and complete native success. The [paired full native audit](../../data/qualification/gates-2026-10-16/retrieval-v5-paired-full-audit.json) passes every run and comparison check, including 120 independent common fields. The 73.33-point matched spread meets D7; retrieval qualification and G05 are cleared.

Turns-v2 / protocol 3 Qwen3.5 run `batch_1b5482fedba46b74e84a5e8c` completed all
48 histories: 182/192 checks (94.79%), 42/48 whole cases (87.5%), one cap, zero unsupported cases. All native identities,
plans, keys and grades independently reproduce; [retained audit](../../data/qualification/gates-2026-10-16/turns-v2-proto3-native-audit.json).
The numeric band passes, but ten quoted categorical UPDATE decoys equaled their final
correct setting, violating the nonmatching-distractor contract. Keys and scores remain valid;
the affected decoys did not discriminate that field. Historical evidence is unchanged.
`turns-v3` / protocol 4, reviewed and deployed on the control node on 2026-10-17, corrects this and strengthens the bounded hard
histories; [current contract and shortcut checks](../../benchmarks/README.md#multi-turn-consistency-part-6-milestone-102).
The 96-check focused regression run passes (one existing Starlette/httpx warning), alongside
independent printed-message/decoy replay. Source SHA-256:
`e1dfa8b1c391982c5308cc3a1f05090c4105c970c856554736dc7a55dfa2aa7a`.
Field grader 3 is unchanged; representative acceptance remains open below. Deployment paused the table
run after eight durable samples; source/plan identity and all eight grades independently
reproduce after resumption. Shared quality and table source hashes stayed unchanged.

Turns-v3 Qwen3.5 run `batch_0799698723b91bd68450e8dd` failed during case seven when an unmocked config test invoked GPU discovery; six saved native cases remained valid and the unfinished case was discarded. After the interference-retry fix was activated at a quiet boundary, normal target `batch_1d3f57f290cbfcaecb980277` completed 48/48 cases on 2026-10-18: 184/192 checks (95.83%), 43/48 whole cases (89.58%), zero caps/unsupported/native failures. The [final lineage audit](../../data/qualification/gates-2026-10-16/turns-continuation-final-audit.json) verifies six exact source receipts plus 42 fresh native cases and the unchanged source snapshot; every [full native audit](../../data/qualification/gates-2026-10-16/turns-v3-qwen-continuation-final-native-audit.json) check passes. The score exceeds D7's 95% strong-model ceiling, so qualification remains open. The turns-v4 / protocol 5 revision changes only eight hard temporary histories, retaining 40 exact cases and all 192 checks. Its [control node delivery](../../data/qualification/gates-2026-10-16/turns-v4-deployment.json) on 2026-10-18 passed source parity, authenticated live protocol 5 and unchanged 48-case/36-task retained-record checks after a quiet WeightRoom-only restart; the ensuing native result is recorded below. [Bounded revision validation](../../data/qualification/gates-2026-10-16/turns-v4-bounded-revision-validation.json). Historical results remain unchanged.

Qwen3.5 v4 run `batch_3f34061304f89d9986790395` completed 48/48 cases: 183/192 checks (95.3125%), 42/48 whole cases, zero caps/unsupported/native failures. Every [full native audit](../../data/qualification/gates-2026-10-16/turns-v4-qwen-full-native-audit.json) check passes; [retained failure review](../../data/qualification/gates-2026-10-16/turns-v4-qwen9-terminal-diagnostics.json) confirms expiry and quoted-update misses. The result is one correct field above the allowed 182/192 ceiling, so it does not clear qualification. A locally validated turns-v5 / protocol 6 revision changes only eight hard record histories while preserving the other 40 cases, all 192 fields and sixteen simple controls; [validation](../../data/qualification/gates-2026-10-16/turns-v5-bounded-revision-validation.json) includes independent printed-history replay and 18 isolated tests. [Control node delivery](../../data/qualification/gates-2026-10-16/turns-v5-front-delivery-20261018.json) on 2026-10-18 verified the one-file source change, live protocol 6 and an empty queue after the WeightRoom-only restart. Qwen2.5 1.5B weak run `batch_08f012363e08dc1beba2048a` completed 48/48 cases at 27/192 checks (14.0625%), with zero caps or native failures; every [independent full audit](../../data/qualification/gates-2026-10-16/turns-v5-qwen15-root-full-native-audit.json) check passes. Fresh Qwen3.5 9B strong run `batch_0c178a67a34c1858ac8568cd` completed 48/48 cases in the normally accepted 65,536-token, one-GPU profile: 182/192 checks (94.79%), 41/48 whole cases, zero caps, unsupported cases or native failures. Every record and comparison check passes in the [independent paired full audit](../../data/qualification/gates-2026-10-16/turns-v5-root-paired-audit-20261018.json), including 48 matched cases and 192 independent common checks. The 80.73-point spread meets D7 and clears Turns qualification. The temporary profile was [restored through a scoped two-field preview/apply](../../data/qualification/gates-2026-10-16/qwen9-65536-profile-restored.json); that restored profile was not tested by this run. The prepared v6 contingency was not adopted. Original milestone-101 native request rejection acceptance is recorded below.

JSON protocol 6 (`json-schema-paired-v5`) completed GPT-OSS run
`batch_ff4c8bb38e88bdf3f248a9c0`: free completion scored 142/162 checks (87.65%),
including 88/108 semantic fields and 27/27 each for bare JSON and schema validity.
All 27 constrained partners executed; semantic accuracy was 88.89%, versus 81.48%
free (+7.41 percentage points). Both halves had zero caps and unsupported cases.
All frozen-plan, coverage, native-receipt and independent regrading checks passed;
[retained audit](../../data/qualification/gates-2026-10-16/json-v6-retained-audit.json).
Qwen2.5-1.5B run `batch_e17912018697f57fa8827912` completed the identical 54-case
plan with 0/162 free headline checks, zero caps and unsupported cases in both halves.
Its free bare-JSON/schema accuracy was 0%; semantic value accuracy was 15.74% in
both halves, while constrained schema validity was 100%. The matched comparison
covers all 108 independent semantic fields and passes the weak/strong targets;
JSON calibration and milestone 103 are cleared under D7. SmolLM2 run
`batch_08d7d6988ebc3fa3e21018e9` remains a cancelled 14-free-case diagnostic
(0/72 checks, three caps), excluded from qualification.

Milestone 101 qualified on 2026-10-18: the paired native JSON evidence clears constrained-format execution; GPT-OSS history and Qwen2.5 boolean-schema requests each produced a retained native HTTP 400 at `/apply-template`, with explicit unsupported status and no generation or fallback. A native JSON-object control returned the requested object with 88 uncached input tokens, 60 output tokens and no cap. The [independent raw-capture audit](../../data/qualification/gates-2026-10-16/m101-root-raw-receipt-audit.json), [source parity](../../data/qualification/gates-2026-10-16/m101-native-source-parity-20261018.json) and [scoped runner receipt](../../data/qualification/gates-2026-10-16/m101-native-runner-receipt.json) verify exact requests, native logs, frozen identities, terminal tasks, revoked temporary key and idle cleanup. Generation-stage rejection is unclaimed. G03 is cleared.
