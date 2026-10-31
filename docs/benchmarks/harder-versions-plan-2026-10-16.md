# Benchmark revision constraints

The completed A3 implementation and qualification steps have been removed from this plan.
Current protocols are maintained in [the benchmark guide](../../benchmarks/README.md);
results and historical limits are in [the qualification audit](qualification-2026-10-15.md).
[Agent backlog](../agent-status.md#proposals) owns future work. The original design and calibration tables
remain in Git at `0242ffff:docs/benchmarks/harder-versions-plan-2026-10-16.md`.

## Owner decisions (approved 2026-10-16)

Standing constraints from the approved A3 design and later
[owner decisions D1–D7](../agents.md#standing-permissions-and-execution-constraints):

- Use the D7 representative-model policy; a full-catalogue assessment is separate.
- Target strong accuracy of 80–95% and weak accuracy below 30%, with at least 100 independent checks and complete matched native coverage.
- Keep four simple standard-tier cases per category in Multi-turn and Reasoning.
- Give Reasoning up to 32,768 output tokens where context and fit permit. Report capped cases separately; more than 10% proven still-reasoning caps disqualifies A3 qualification.
- Preserve Hierarchy’s poisoned-history and user-permitted coverage and four-check case contract.
- Derive keys deterministically in Python; never use model judges or execute generated code. Scripted lazy answerers must score at most 50%.
- Version changed cases through both protocol and dataset identifiers. Refuse mixed-version continuation, preserve old evidence and explicitly version grading changes.
- Preserve legacy-case grading unless an explicitly versioned correction is required. Summaries must retain older samples’ category splits.
- D2 permits bounded revision without renewed approval when a version misses its target; never silently retune a frozen protocol.
- Freeze queued/running implementations, model identities and environments. Change shared `benchmarks/quality.py` only after affected work drains; it contributes to every quality implementation hash.

No A3 implementation, sweep or qualification task remains in this document.
