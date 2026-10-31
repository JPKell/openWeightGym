"""Roles: a caller names a kind of work, and LoadCoach picks the model.

A role is a name, such as `chat` or `code`, and a list of models, each with a weight the owner
gives it. A caller sends the role's name where a model's name would go. Naming a model
outright works as before: a role is another way to ask, beside it.

The name can also say how much the pick matters:

    chat             preference score with a 25% bonus for a ready resident local model
    chat:best        the top of the list, waited for, with idle models pushed off to load it
    chat:available   the highest in the list that is warm: loaded, with room for a request now
    chat:fast        speed preference with a stronger ×2 local residency weight

This file keeps the roles and holds the rule that picks. What can be known of each model at
the moment of a request, whether it is warm, how fast it is, is gathered in app.py and
handed to `pick`.
"""

from __future__ import annotations

import json
import math
import re

from db import DatabaseSource, database, migrate
from db.schemas import ROLES

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")  # a role's name. No colon in it: that is what sets a strength apart
STRENGTHS = ("best", "available", "fast")
LOADED_BONUS = 25  # percent added to a ready resident local model's preference for an ordinary role request
FAST_LOADED_BONUS = 100  # :fast weights a ready local resident twice as strongly by default
ANSWER = 400  # the tokens an answer is taken to have, when working out how soon a model would be done
UNKNOWN_SPEED = 10.0  # tokens a second assumed for a model that has not answered yet, so that one that has is preferred
UNKNOWN_LOAD = 60.0  # seconds assumed to load a model that was never loaded


def check_weights(weights: dict) -> None:
    """Reject invalid weights before they reach storage or JSON transport."""
    if not all(type(weight) in (int, float) and 0 <= weight <= 1e100
               and (type(weight) is int or math.isfinite(weight)) for weight in weights.values()):
        raise ValueError("A role weight must be a finite number from 0 to 1e100. Higher weights are preferred; 0 leaves a model out.")


def benchmark_shares(weights: dict) -> dict[str, float]:
    """Relative influence within one role, with zero-weight benchmarks ignored."""
    selected = {key: weight for key, weight in weights.items() if weight > 0}
    scale = max(selected.values(), default=0)
    total = sum(weight / scale for weight in selected.values()) if scale else 0
    return {key: (weight / scale) / total for key, weight in selected.items()}


def check_loaded_bonuses(settings: dict) -> None:
    """Check per-role reuse preferences before saving or serializing form values."""
    for key, default, maximum, label in (('loaded_bonus', LOADED_BONUS, 100, 'Loaded-model'),
                                         ('fast_loaded_bonus', FAST_LOADED_BONUS, 500, 'Fast loaded-model')):
        bonus = settings.get(key, default)
        if type(bonus) not in (int, float) or not math.isfinite(bonus) or not 0 <= bonus <= maximum:
            raise ValueError(f'{label} bonus must be a finite percentage from 0 to {maximum}; 0 disables it.')


def check_role(name: str, settings: dict, models: set[str]) -> None:
    """Raise ValueError, saying why, if these could not be a role's settings. `models` are the names of the models there are, local and outside."""
    if not NAME.fullmatch(name):
        raise ValueError(f"{name!r}: a role's name is letters, digits, dots and dashes, with no colon.")
    if name in models:
        raise ValueError(f"There is already a model named {name!r}.")
    listed = settings.get("models") if isinstance(settings, dict) else None
    if not isinstance(listed, dict) or settings.keys() - {"models", "benchmarks", "missing", "stale", "max_age_days", "requires", "loaded_bonus", "fast_loaded_bonus"}:
        raise ValueError(f"role {name!r} sets models: each model's name with its weight.")
    check_weights(listed)
    check_loaded_bonuses(settings)
    if unknown := sorted(listed.keys() - models):
        raise ValueError(f"role {name!r}: there is no model named {', '.join(unknown)}.")
    benchmarks = settings.get("benchmarks", {})
    if not isinstance(benchmarks, dict) or not all(isinstance(key, str) and NAME.fullmatch(key) for key in benchmarks):
        raise ValueError("Benchmarks must map benchmark IDs to weights.")
    check_weights(benchmarks)
    if settings.get("missing", "manual") not in ("manual", "exclude"):
        raise ValueError("Missing evidence policy must be manual or exclude.")
    if settings.get("stale", "warn") not in ("warn", "exclude"):
        raise ValueError("Stale evidence policy must be warn or exclude.")
    age = settings.get("max_age_days", 0)
    if type(age) not in (int, float) or not 0 <= age <= 36500 or not math.isfinite(age):
        raise ValueError("Evidence age must be a finite number from 0 to 36500 days; 0 disables the age limit.")
    needs = settings.get("requires", {})
    if not isinstance(needs, dict) or needs.keys() - {"pictures", "tools", "tokens", "stays"}:
        raise ValueError("Role requirements are pictures, tools, tokens and stays.")
    if any(type(needs[key]) is not bool for key in needs.keys() - {"tokens"}) or ("tokens" in needs and (type(needs["tokens"]) is not int or not 0 <= needs["tokens"] <= 100000000)):
        raise ValueError("Requirements use booleans and a non-negative integer context token count.")


def ordered(settings: dict, scores: dict[str, float] | None = None) -> list[str]:
    """A role's models, the one to prefer first: by score times the owner's weight. One with weight 0 is left out.

    Until a benchmark gives scores, every score is 1 and the weights alone decide. Models
    that come out equal keep the order they were listed in.
    """
    worth = {model: (scores or {}).get(model, 1) * weight for model, weight in settings["models"].items() if weight > 0}
    return sorted(worth, key=lambda model: -worth[model])


def seconds_to_answer(candidate: dict) -> float:
    """How soon a model would be done with an ordinary answer: the time to load it, unless it is warm, and the time to write the answer."""
    if candidate.get('estimate', {}).get('seconds') is not None:
        return candidate['estimate']['seconds']
    waiting = 0 if candidate["warm"] or candidate.get('resident') else candidate["load"] if candidate["load"] is not None else UNKNOWN_LOAD
    seconds = waiting + ANSWER / (candidate["speed"] or UNKNOWN_SPEED)
    return min(seconds, float.fromhex('0x1.fffffffffffffp+1023'))


def routing_score(candidate: dict) -> float:
    """Ordinary-role preference after the local residency multiplier; quality order stays separate."""
    return candidate['score'] * candidate.get('loaded_factor', 1)


def loaded_factors(settings: dict, resident: bool) -> dict:
    """The two reuse weights, shared by live routing and the hypothetical Dry fit preview."""
    return {'loaded_factor': 1 + settings.get('loaded_bonus', LOADED_BONUS) / 100 if resident else 1,
            'fast_loaded_factor': 1 + settings.get('fast_loaded_bonus', FAST_LOADED_BONUS) / 100 if resident else 1}


def fast_preference_seconds(candidate: dict) -> float:
    """Timing preference with a residency discount; never report it as elapsed-time prediction."""
    return seconds_to_answer(candidate) / candidate.get('fast_loaded_factor', 1)


def pick(strength: str | None, candidates: list[dict]) -> tuple[str, str] | None:
    """Choose among a role's models for one request. Gives the model's name and why, in a few words. None if there is none to choose.

    `candidates` are the models that can answer this request at all, in the role's order. Each
    has `name`; `warm`, true if it is loaded and has room for a request now; `load`, the
    seconds it last took to load, or None; and `speed`, the tokens a second it has been
    answering at, or None. `score` is weighted quality/reliability preference, and
    `loaded_factor` is the role's residency multiplier (1 for a cold or hosted model).
    """
    if not candidates:
        return None
    top, warm = candidates[0], [one for one in candidates if one["warm"]]
    if strength == "best":
        return top["name"], "the top of the list"
    if strength == "available":
        return (warm[0]["name"], "the highest in the list that is warm") if warm else (top["name"], "nothing in the list is warm, so the top of it")
    if strength == "fast":
        soonest = min(candidates, key=lambda one: ('estimate' in one and one['estimate']['seconds'] is None,
                                                  fast_preference_seconds(one), -one.get('fast_loaded_factor', 1)))
        how = "loaded" if soonest.get('resident') else "warm" if soonest["warm"] else "to be loaded"
        factor = soonest.get('fast_loaded_factor', 1)
        if 'estimate' in soonest:
            timing = (f"about {soonest['estimate']['seconds']:g} s including queue, load, prompt and output budgets"
                      if soonest['estimate']['seconds'] is not None else "timing unavailable; provisional preference only")
            return soonest['name'], f"best speed preference: {timing}; {how}; loaded reuse x{factor:g}"
        return soonest["name"], (f"best speed preference: about {seconds_to_answer(soonest):.0f} s before queue/prompt processing, "
                                f"{how}, at {soonest['speed'] or UNKNOWN_SPEED:.0f} tokens a second; "
                                f"loaded reuse x{factor:g}, weighted comparison {fast_preference_seconds(soonest):.1f}")
    chosen = max(candidates, key=lambda one: (routing_score(one), one.get('loaded_factor', 1) > 1))
    factor = chosen.get('loaded_factor', 1)
    reason = (f"ready and loaded locally; {(factor - 1) * 100:g}% loaded-model bonus"
              if factor > 1 else 'highest preference after loaded-model bonuses')
    return chosen['name'], reason


class Roles:
    """The roles, kept in the database. A change is written at once and counts from the next request."""

    def __init__(self, path: DatabaseSource) -> None:
        """Open routing role storage and apply its portable schema migrations."""
        source = database(path)
        self.db = source.connect(check_same_thread=False)
        migrate(source, self.db, "loadcoach.roles", ROLES)

    def names(self) -> list[str]:
        """List configured routing roles in operator-defined order."""
        return [name for (name,) in self.db.execute("SELECT name FROM roles ORDER BY position, name")]

    def get(self, name: str) -> dict | None:
        """Read one routing role's retained configuration, or None when absent."""
        row = self.db.execute("SELECT settings FROM roles WHERE name = ?", (name,)).fetchone()
        return row and json.loads(row[0])

    def put(self, name: str, settings: dict) -> None:
        """Add a role, or replace the settings of one that exists. Check them first with check_role."""
        with self.db:
            self.db.execute("INSERT INTO roles (name, settings, position) VALUES (?, ?, COALESCE((SELECT MAX(position)+1 FROM roles),1)) ON CONFLICT (name) DO UPDATE SET settings = excluded.settings", (name, json.dumps(settings)))

    def remove(self, name: str) -> bool:
        """Delete the selected routing role and report whether it existed."""
        with self.db:
            return self.db.execute("DELETE FROM roles WHERE name = ?", (name,)).rowcount > 0

    def renamed(self, name: str, new: str) -> list[str]:
        """A model was renamed: every role that lists it follows it to its new name, in the same place. Gives the roles that did."""
        followed = []
        for role in self.names():
            settings = self.get(role)
            if name in settings["models"]:
                self.put(role, {**settings, "models": {new if model == name else model: weight for model, weight in settings["models"].items()}})
                followed.append(role)
        return followed

    def asked(self, name: str) -> tuple[str, str | None] | None:
        """A role and a strength, if that is what a name asks for: `chat`, or `chat:best`. None for any other name."""
        role, colon, strength = name.partition(":")
        if self.get(role) is None or (colon and strength not in STRENGTHS):
            return None
        return role, strength or None
