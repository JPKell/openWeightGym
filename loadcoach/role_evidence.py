"""Compact client-published measurements; no benchmark execution or network reads here."""

import json
import math
import time

from db import adopt, migrate
from db.schemas import ROLE_EVIDENCE

from loadcoach.roles import benchmark_shares


def finite(value):
    """Return whether the value is a finite numeric measurement."""
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


class RoleEvidence:
    """Validated benchmark definitions and results consumed by role selection."""
    def __init__(self, db):
        """Bind the evidence store and apply the role-evidence schema migrations."""
        self.db = db = adopt(db)
        migrate(db.database, db, "loadcoach.role_evidence", ROLE_EVIDENCE)

    @staticmethod
    def validate(snapshot):
        """Reject malformed role-evidence snapshots before retaining any records."""
        if not isinstance(snapshot, dict) or snapshot.keys() - {"definitions", "results"}:
            raise ValueError("Publish definitions and results as a snapshot object.")
        definitions, results = snapshot.get("definitions"), snapshot.get("results")
        if not isinstance(definitions, list) or not isinstance(results, list) or len(definitions) > 100 or len(results) > 5000:
            raise ValueError("A snapshot allows at most 100 definitions and 5000 results.")
        for definition in definitions:
            if not isinstance(definition, dict) or not all(isinstance(definition.get(key), str) and 0 < len(definition[key]) <= 500 for key in ("id", "name", "formula")) or definition.get("direction") not in ("high", "low") or type(definition.get("version")) is not int or not 1 <= definition["version"] <= 2147483647:
                raise ValueError("Each definition needs an ID, name, formula, direction (high/low) and positive version.")
        ids = {one["id"] for one in definitions}
        for result in results:
            if not isinstance(result, dict) or not all(isinstance(result.get(key), str) and 0 < len(result[key]) <= 200 for key in ("model_id", "fingerprint", "benchmark", "run")) or result["benchmark"] not in ids:
                raise ValueError("Each result must identify its model, fingerprint, benchmark definition and run.")
            if not finite(result.get("measured_at")) or result["measured_at"] < 0 or result["measured_at"] > time.time() + 300 or not finite(result.get("score")) or type(result.get("version")) is not int or not 0 <= result["version"] <= 2147483647:
                raise ValueError("Results need a finite score, valid timestamp and non-negative version.")
            if not isinstance(result.get('formula'), str) or len(result['formula']) > 500:
                raise ValueError("Results must record the score formula.")
            if not all(result.get(key) is None or isinstance(result[key], str) and len(result[key]) <= 200 for key in ("engine_hash", "machine_hash")):
                raise ValueError("Environment hashes must be strings.")
            for key in ("speed", "load"):
                if result.get(key) is not None and (not finite(result[key]) or result[key] < 0):
                    raise ValueError("Timing evidence must be finite and non-negative.")
        # Store only the public compact contract, never arbitrary client data or credentials.
        return {"definitions": [{key: one[key] for key in ("id", "name", "formula", "direction", "version")} for one in definitions],
                "results": [{key: float(one[key]) if key == 'score' else one.get(key) for key in ("model_id", "fingerprint", "benchmark", "run", "measured_at", "score", "formula", "version", "engine_hash", "machine_hash", "speed", "load")} for one in results]}

    def accept(self, snapshot, *, definitions=True):
        """Persist a validated snapshot of benchmark definitions and results."""
        snapshot = self.validate(snapshot)
        with self.db:
            for definition in snapshot["definitions"] if definitions else []:
                self.db.execute("INSERT INTO role_benchmarks VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET definition=excluded.definition,published=excluded.published",
                                (definition["id"], json.dumps(definition), time.time()))
            for one in snapshot["results"]:
                self.db.execute("INSERT INTO role_evidence VALUES(?,?,?,?,?,?) ON CONFLICT(model_id,fingerprint,benchmark,version) DO UPDATE SET measured=excluded.measured,evidence=excluded.evidence WHERE excluded.measured >= role_evidence.measured",
                                (one["model_id"], one["fingerprint"], one["benchmark"], one["version"], one["measured_at"], json.dumps(one)))

    def definitions(self):
        """Return the retained benchmark definitions available to role selection."""
        return [json.loads(row[0]) for row in self.db.execute("SELECT definition FROM role_benchmarks ORDER BY id")]

    def rank(self, settings, identities, environment, now=None):
        """Return scores and reasons, normalizing only among permitted role candidates."""
        now = time.time() if now is None else now
        definitions = {one["id"]: one for one in self.definitions()}
        selected = {key: weight for key, weight in settings.get("benchmarks", {}).items() if weight > 0}
        shares = benchmark_shares(selected)
        details = {name: {"quality": None, "score": weight, "evidence": [], "evidence_excluded": False} for name, weight in settings["models"].items() if weight > 0}
        for key, weight in selected.items():
            definition = definitions.get(key)
            usable = {}
            for name, detail in details.items():
                identity, fingerprint = identities.get(name, (None, None))
                row = self.db.execute("SELECT evidence FROM role_evidence WHERE model_id=? AND fingerprint=? AND benchmark=? AND version=?",
                                      (identity, fingerprint, key, definition["version"] if definition else -1)).fetchone()
                result = json.loads(row[0]) if row else None
                if result and result.get('formula') != definition['formula']:
                    result = None
                warnings = []
                if result:
                    warnings = [f"{field.removesuffix('_hash')} changed or unknown; rerun recommended" for field in ("engine_hash", "machine_hash") if not environment.get(field) or environment[field] != result.get(field)]
                    if settings.get("max_age_days", 0) and now - result["measured_at"] > settings["max_age_days"] * 86400:
                        warnings.append("evidence exceeds the age limit; rerun recommended")
                point = {"benchmark": key, "weight": weight, "raw": result["score"] if result else None, "normalized": None,
                         "share": shares[key], "contribution": None,
                         "speed": result.get("speed") if result else None, "load": result.get("load") if result else None,
                         "run": result["run"] if result else None, "warnings": warnings,
                         "reason": "no compatible result for the current model fingerprint and benchmark version" if not result else "stale evidence excluded" if warnings and settings.get("stale", "warn") == "exclude" else "accepted"}
                detail["evidence"].append(point)
                if point["reason"] == "accepted" and name in identities:
                    usable[name] = result["score"]
            if usable:
                low, high = min(usable.values()), max(usable.values())
                for name, value in usable.items():
                    # Scale before subtracting to avoid overflow with large finite scores.
                    scale = max(abs(low), abs(high), 1)
                    normalized = .5 if high == low else (value / scale - low / scale) / (high / scale - low / scale)
                    if definition["direction"] == "low":
                        normalized = 1 - normalized
                    details[name]["evidence"][-1]["normalized"] = normalized
        for name, detail in details.items():
            points = detail["evidence"]
            if points and all(one["normalized"] is not None for one in points):
                for one in points:
                    one['contribution'] = one['normalized'] * one['share']
                quality = sum(one['contribution'] for one in points)
                detail.update(quality=quality, score=settings["models"][name] * (1 + quality))
            elif points:
                detail["evidence_excluded"] = settings.get("missing", "manual") == "exclude"
                detail["fallback"] = "excluded: selected measurements are missing or unusable" if detail["evidence_excluded"] else "manual weight: selected measurements are missing or unusable"
        return details
