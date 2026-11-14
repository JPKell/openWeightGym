<!--docs
title = "Manage scoring"
summary = "Change formulas and benchmark availability using retained measurements."
order = 60
covers = ["/benchmarks/manage"]
-->
# Manage scoring

Change how retained measurements become benchmark scores.

## Before you start

Use an account allowed to manage benchmarks; see generated [abilities](/docs/system/abilities). Formula changes reinterpret retained evidence.

## Do it

1. Open **Benchmark → Manage**, search **Find a benchmark** and select its definition.
2. Edit **Score calculation** using recorded values, numbers, parentheses and offered arithmetic operators. Select a **Recorded values** entry to insert it at the cursor.
3. Set **Green represents**, then choose **Save settings**. **Discard changes** restores the accepted definition.

## Options

| Control | Effect |
| --- | --- |
| **Include in available benchmarks** | Offers the definition for new work. |
| **Model server** | Quality suites can reuse a server or load a fresh one per case. Each case remains a separate request with its prompt cache off. |
| **Score calculation** | Rescores saved measurements without rerunning models. Missing operands produce no score. |
| **Measurement protocol** | Explains the selected benchmark; use recorded units when composing formulas. |

Resource measurements always load a fresh server. Execution-mode changes apply to runs started after saving. Choosing a definition or discarding a draft runs no inference.

## Limits and troubleshooting

A valid formula can lack operands for some runs. Inspect recorded values before treating an empty score as model failure.

## Related

[What is measured](/docs/benchmarks/measures), [Node measurements](/docs/benchmarks/node-measurements), [Compare](/docs/benchmarks/compare).
