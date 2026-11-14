<!--docs
title = "Compare"
summary = "Choose models and recorded fingerprints for side-by-side benchmark evidence."
order = 50
covers = ["/benchmarks/compare"]
-->
# Compare

Compare saved benchmark scores and measurements across models and fingerprints.

## Do it

1. Open **Benchmark → Compare** and select models in **Choose models**.
2. Optionally expand **Recorded fingerprints**, choose its filter and select saved runs for other fingerprints, quants or removed models.
3. Choose benchmarks to fill **Comparison**. Open a score to inspect its run, or a fingerprint to inspect its permanent record.

Without JavaScript, choose **Update comparison** after changing the selection.

## Options

| Item | Meaning |
| --- | --- |
| **Rank** | Uses score and green direction across models available to the account. Ties share a rank. |
| **Average rank** | Uses available selected benchmarks; its count shows coverage. An asterisk marks missing results another selected model has. |
| **Older version · unranked** | A retained older benchmark version is visible but excluded from ranking. |
| Changed settings | Results can still rank; inspect frozen settings before treating them as current behavior. |

## Limits and troubleshooting

Choose at least one model and benchmark. Other-fingerprint and other-quant filters need a current model selection. Missing values are not failed scores; a better average over fewer benchmarks is not complete coverage.

## Related

[Scores and results](/docs/benchmarks/scores), [Fingerprints](/docs/model-guides/fingerprints), [Manage scoring](/docs/benchmarks/manage).
