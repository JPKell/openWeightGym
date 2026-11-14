# Arithmetic-v3 local validation

Implemented **2026-10-14** after the [Part D audit](arithmetic-audit.md).
Delivery and qualification are recorded under milestone 80 in the [agent ledger](../agent-status.md#milestones).

## Case contract

`benchmarks/suites/arithmetic.py` generates the same cases from seed **20261014**, irrespective
of context size. Context changes only transport budgets. Dataset and protocol are both v3;
the shared field grader stays at version 2. No model judges answers or executes model output.

| Category | Cases | Scored fields | Questions |
|---|---:|---:|---|
| Word problems | 8 | 24 | Item subtotal, discounted/taxed/shipped share, remaining personal budget |
| CSV tables | 8 | 32 | Filtered count and unit sum, greatest row revenue, winning team's total revenue |
| Unit conversion | 8 | 24 | Three mixed-unit totals per case, rotating duration, distance, mass and payments |
| Percent changes | 8 | 24 | Signed change, price before a rise, inventory after compounded rise/fall |
| Calendar | 8 | 24 | February-to-March elapsed days, English weekday, shifted ISO date |
| Total | 40 | 128 | Separately scored fields; headline remains field accuracy |

Fraction calculations reject nonintegral keys rather than round. Money is integer cents;
signed percentage changes are integer tenths of a percent. Tables have thirty unique rows,
varied filter counts and explicit lexical tie-breaking. Four calendar cases cross a leap day
and four cross an ordinary February. Persons, places, dates and record identifiers are unique
across cases. Questions request flat named fields and one final object, then stop.

Only `case['request']` is sent to the existing native task probe. Expected answers and source
facts remain in the control node's frozen plan/evidence. Native decoding is unconstrained; reasoning,
raw output, token counts and cap flags remain available under the existing protocol.

## Verification and limits

The benchmark and documentation checks passed **78 tests** across the broad run and the
corrected forty-case preemption regression rerun. One existing Starlette/httpx deprecation
warning remains. No full Suite test run or production deployment was performed for this change.

- Every answer key scores 100%; replacing any requested field with a boolean fails that field.
- Separate Decimal invoices and SQL queries over the actual CSV verify purchase/table keys.
  Inverse unit constraints, percentage cross-multiplication and calendar/ordinal checks verify
  the remaining categories. Repeated generation and differing context budgets preserve cases.
- Scripted null answers score **0/128 (0%)**. A shortcut solver ignores discount/tax, reads
  only ten table rows, skips conversions, adds successive percentages and assumes 28 days in
  February: **19/128 (14.8%)**. Neither passes a whole case.
- Regression fixtures preserve the audited saved-answer credit. A temporary-store check
  retains a historical v2 result and score, reports current v3 separately, and rejects old-plan
  continuation without issuing a native request. Existing end-to-end preemption/resume checks
  complete all forty cases, retain earlier samples, and keep keys away from compute.
- An isolated **CPU-only** SmolLM2 135M Q4 native smoke check completed one case per category
  on loopback, with `--device none --gpu-layers 0 --no-kv-offload --no-op-offload`, two CPU
  threads and context 2,048. Prompt token counts were 191, 703, 129, 205 and 160. Its shortened
  **256-token** cap was reached by four of five answers; it earned one of sixteen fields.
  This verifies transport and question fit only. It is not a full protocol-v3 run or model
  ranking evidence, and no production benchmark result was written.

Local implementation does not establish the required best-model 80–95%, smallest-model
under-30% distribution. Deliver the control node benchmark files, then run the four-model check with
the current full answer allowance when the owner authorizes GPU availability. Inspect raw
answers, cap flags and serving identity before interpreting a low score; delimiter loops in
the audited v2 runs cannot be assumed to disappear with additional answer room.
