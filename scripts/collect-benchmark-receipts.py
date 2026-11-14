#!/usr/bin/env python3
"""Read frozen G04/G17 benchmark receipts from the control node's SQLite store without modifying it.

Run on the control node from a matching openWeightGym checkout after the runs finish. The output omits answers,
prompts, logs, task tokens and API keys. A false check is a review target, not an auto-retry.
"""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import time

from benchmarks import MODULES
from benchmarks.scoring import calculate


KINDS = {"embedding_context", "embedding_speed", "embedding_load"}


def receipt(db, kind, run_id, expected_model, expected_state):
    """Build one portable verification receipt from a retained benchmark run."""
    row = db.execute(
        "SELECT model,model_id,benchmark,fingerprint,subject,environment,state,created,started,ended "
        "FROM benchmark_runs WHERE id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"No benchmark run {run_id}")
    model, model_id, actual_kind, fingerprint, subject_json, environment_json, state, created, started, ended = row
    if actual_kind != kind:
        raise ValueError(f"{run_id} is {actual_kind}, not {kind}")
    subject, environment = json.loads(subject_json), json.loads(environment_json)
    samples = []
    full_samples = []
    for sample_id, values_json, evidence_json in db.execute(
        "SELECT s.sample,s.measurements,coalesce(e.evidence,'{}') FROM benchmark_samples s "
        "LEFT JOIN benchmark_evidence e ON e.run=s.run AND e.sample=s.sample "
        "WHERE s.run=? ORDER BY s.at,s.sample", (run_id,)
    ):
        values, evidence = json.loads(values_json), json.loads(evidence_json)
        full_samples.append({"id": sample_id, "values": values, "evidence": evidence})
        kept = {key: values.get(key) for key in (
            "success", "server_reused", "context_tokens", "input_tokens", "output_tokens",
            "embedding_dimensions", "request_seconds", "embedding_tokens_per_second",
            "embeddings_per_second", "load_seconds", "peak_gpu_mib", "error") if key in values}
        proof = {key: evidence.get(key) for key in (
            "endpoint", "usage", "cache_prompt", "prompt_tokens_sha256", "embedding_sha256",
            "embedding_dimensions") if key in evidence}
        samples.append({"id": sample_id, "values": kept, "evidence": proof})
    definition = db.execute("SELECT formula,server FROM benchmark_definitions WHERE id=?", (kind,)).fetchone()
    if definition is None:
        raise ValueError(f"No definition for {kind}")
    formula, selected_server = definition
    summarized = MODULES[kind].summarize(full_samples)
    score = calculate(formula, {**subject.get("values", {}), **summarized}) if state == "completed" else None
    chosen_server = subject.get("server") or selected_server or ("fresh" if kind in KINDS else "reuse")
    checks = {
        "expected_terminal_state": state == expected_state and ended is not None,
        "expected_model": model == expected_model,
        "frozen_identity": bool(model_id and fingerprint and subject.get("model_id") and
                                subject.get("model_id") == model_id),
        "frozen_environment": bool(environment.get("engine_hash") and environment.get("machine_hash")),
        "samples_retained": bool(samples),
        "score_contract": (isinstance(score, (int, float)) and score > 0) if expected_state == "completed"
                          else score is None and len(samples) >= 2,
    }
    if kind in KINDS:
        succeeded = [sample for sample in samples if sample["values"].get("success") is True]
        checks["fresh_resource_servers"] = chosen_server == "fresh" and all(
            not sample["values"].get("server_reused", False) for sample in samples)
        checks["successful_sample"] = bool(succeeded)
        if kind != "embedding_load":
            checks["native_complete_vectors"] = bool(succeeded) and all(
                sample["values"].get("embedding_dimensions", 0) > 0 and
                sample["values"].get("output_tokens") == 0 and
                sample["evidence"].get("endpoint") == "/v1/embeddings" and
                sample["evidence"].get("usage", {}).get("prompt_tokens") == sample["values"].get("input_tokens") and
                bool(sample["evidence"].get("prompt_tokens_sha256")) and
                bool(sample["evidence"].get("embedding_sha256"))
                for sample in succeeded)
            checks["stable_dimensions"] = len({
                sample["values"].get("embedding_dimensions") for sample in succeeded}) == 1
        else:
            checks["readiness_only"] = all("embedding_dimensions" not in sample["values"] for sample in samples)
    else:
        checks["selected_server_frozen"] = chosen_server in ("reuse", "fresh")
        if chosen_server == "reuse":
            checks["reuse_observed"] = len(samples) >= 2 and not samples[0]["values"].get("server_reused", False) and any(
                sample["values"].get("server_reused") is True for sample in samples[1:])
        else:
            checks["fresh_observed"] = bool(samples) and all(
                not sample["values"].get("server_reused", False) for sample in samples)
    return {
        "run_id": run_id, "benchmark": kind, "model": model, "model_id": model_id,
        "fingerprint": fingerprint, "state": state, "created": created, "started": started,
        "ended": ended, "version": subject.get("protocol", {}).get("version"),
        "server_choice_frozen": chosen_server, "environment_hashes": {
            "engine_hash": environment.get("engine_hash"), "machine_hash": environment.get("machine_hash")},
        "score": score, "score_formula": formula, "metrics": summarized,
        "samples": samples, "checks": checks,
    }


def main():
    """Collect selected retained benchmark receipts without launching model work."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True, help="control node benchmarks.db path")
    parser.add_argument("--model", required=True, help="expected model name on each run")
    parser.add_argument("--run", action="append", required=True, metavar="KIND=ID")
    parser.add_argument("--expected-state", choices=("completed", "cancelled"), default="completed",
                        help="cancelled permits a retained partial G04 quality run")
    parser.add_argument("--output", type=Path, required=True, help="new private JSON evidence path")
    args = parser.parse_args()
    runs = []
    for entry in args.run:
        kind, separator, number = entry.partition("=")
        if not separator or kind not in MODULES or not number:
            parser.error("--run must be a built-in benchmark KIND=ID")
        runs.append((kind, number))
    uri = args.database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=3) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        receipts = [receipt(db, kind, number, args.model, args.expected_state) for kind, number in runs]
    embed = [row for row in receipts if row["benchmark"] in KINDS]
    same_identity = len({(row["model_id"], row["fingerprint"],
                          row["environment_hashes"]["engine_hash"],
                          row["environment_hashes"]["machine_hash"]) for row in embed}) <= 1
    result = {"collected_at_unix": time.time(), "database": str(args.database),
              "expected_model": args.model, "expected_state": args.expected_state,
              "embedding_same_frozen_identity": same_identity,
              "runs": receipts, "checks_passed": same_identity and all(
                  all(row["checks"].values()) for row in receipts)}
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(result, file, indent=2, sort_keys=True)
        file.write("\n")
    print(json.dumps({"output": str(args.output), "runs": len(receipts),
                      "checks_passed": result["checks_passed"]}))


if __name__ == "__main__":
    main()
