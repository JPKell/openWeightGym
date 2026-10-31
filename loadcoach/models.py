"""The models LoadCoach may run.

They are kept in compute's core database, not in the configuration file, so that one can be
added, changed or removed while LoadCoach runs. Like a change to a user, a change to a model is
written at once and applies to the next request. Nothing is restarted and no answer is cut off.

A model here has a stable ID, a public name and its settings: `flags`, which are llama-server's own, and optionally
`gpus`, the cards it needs, `alone`, true for a model that shares its cards with no other, and
`adapters`, the LoRA adapters it is built with, and `min_context`, a lower bound for fitting
the context to available GPU memory. `flags.ctx-size` remains the preferred maximum.

An adapter is a small file that changes how the model behaves. A model with adapters is a
model of its own, with a name and a fingerprint of its own, and a request for it always gets
its adapters, at the strength set here. Models that differ in nothing but their adapters are
kin: one server answers for all of them, and is told for each request which adapters apply.

A projector is a file that lets a model see pictures, named by the flag `mmproj`. A model
with one is a model of its own too, and is kin of the same model without: a server that has
the projector answers for both. `kin_base()` is what kin have in common.

Beside its settings a model has what it measured: the memory its server was seen to use on each
of its cards. LoadCoach writes that itself, after a load. See runner.py.

A model also has a fingerprint: one short text that changes when anything about the model
does. It is the SHA-256 of the model's settings, written one fixed way, with the SHA-256 of
its file in the place of the file's path, plus a revision incremented when a different file
is explicitly selected. Renaming the public label changes neither ID nor fingerprint.
Changing file contents or selecting another filename creates a different fingerprint.

What each fingerprint stood for is written down the first time it is seen, in the table
`fingerprints`, and never removed. A fingerprint in the log or in a benchmark's results can
be read back there after the model is gone from the list and its file from the drive.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time

from db import DatabaseSource, database, migrate
from db.schemas import MODELS
from loadcoach.gguf import facts, read_metadata
from loadcoach import presets as shared_presets

# Compatibility names remain public; model persistence is engine-independent.
from engines import engine_for
from engines.llama_cpp.model import (
    OUR_FLAGS as OUR_FLAGS, FILES as FILES, ADAPTER as ADAPTER,
    ADAPTER_FLAGS as ADAPTER_FLAGS, flags_in as flags_in,
    lora_field as lora_field, request_context as request_context,
    FIRST_PLACE, NAME, check_order,
)


def check_model(name: str, settings: dict, cards: tuple[str, ...]) -> None:
    """Reject model configuration that violates the engine contract."""
    engine_for(settings).model.check_model(name, settings, cards)


def effective(settings: dict, defaults: dict) -> dict:
    """Apply only the selected engine's defaults, never another engine's launch flags."""
    return engine_for(settings).model.effective(settings, defaults)


def files_of(settings: dict) -> dict[str, str]:
    """Return the model, projector and adapter paths referenced by the settings."""
    return engine_for(settings).model.files_of(settings)


def file_selection(settings: dict) -> tuple:
    """Return the file choices used to distinguish model configurations."""
    return (engine_for(settings).id, engine_for(settings).model.file_selection(settings))


def base(settings: dict) -> dict:
    """Return model settings with adapter-specific differences removed."""
    return engine_for(settings).model.base(settings)


def kin_base(settings: dict) -> dict:
    """Return the settings that identify models eligible to share a server."""
    return engine_for(settings).model.kin_base(settings)


def contexts(settings: dict) -> list[int]:
    """Return the supported minimum, midpoint and maximum context sizes."""
    return engine_for(settings).model.contexts(settings)


def at_context(settings: dict, context: int) -> dict:
    """Return model settings adjusted to the requested context size."""
    return engine_for(settings).model.at_context(settings, context)


def memory_base(settings: dict) -> dict:
    """Return the settings used to key model memory measurements."""
    return engine_for(settings).model.memory_base(settings)


class Models:
    """The models table. Every change is written at once and applies to the next request."""

    def __init__(self, path: DatabaseSource, defaults: dict | None = None, first: dict[str, dict] | None = None) -> None:
        """`defaults` are flags every model gets unless it sets its own. `first` are the models
        to start with, used once, when the table is empty: that is how the models that used to
        be written in the configuration file are taken over."""
        self.defaults = defaults or {}
        source = database(path)
        self.db = source.connect(check_same_thread=False)
        migrate(source, self.db, "loadcoach.models", MODELS)
        self._identities()
        self.remembered: set[str] = set()  # the fingerprints known to be written down, so that the table is not asked at every request
        self.taken_over = 0  # how many models were taken over from `first` just now
        if first and not self.names():
            for settings in first.values():
                check_order(settings.get("order"))
            for name, settings in first.items():
                self.put(name, {key: value for key, value in settings.items() if key != "order"})
                self.set_place(name, settings.get("order"))
            self.taken_over = len(first)

    def names(self) -> list[str]:
        """Every model's name, in the order models are listed everywhere, `GET /v1/models` among them.

        By sort order first, the lowest number first. Models with the same sort order go by
        name, whatever the case of its letters. Every model starts with FIRST_PLACE, so
        until someone gives a model another number the list is by name.
        """
        return [name for (name,) in self.db.execute("SELECT name FROM models ORDER BY COALESCE(place, ?), LOWER(name), name", (FIRST_PLACE,))]

    def _identities(self) -> None:
        """Migrate model rows once, retaining timestamped names and deletion boundaries."""
        if "events" in self.db.database.tables(self.db):
            events = self.db.execute("SELECT at,what,model,detail FROM events WHERE kind='model' AND what IN ('renamed','added','removed') ORDER BY id DESC").fetchall()
        else:
            events = []
        with self.db:
            for name, in self.db.execute('SELECT name FROM models WHERE identity IS NULL').fetchall():
                identity = 'model_' + secrets.token_hex(16)
                self.db.execute('UPDATE models SET identity=? WHERE name=? AND identity IS NULL', (identity,name))
                actual = self.identity(name)
                if actual != identity:
                    continue
                self.db.execute('INSERT INTO model_identities VALUES (?,0,NULL)', (identity,))
                alias, upper, periods = name, None, []
                for at, what, model, detail in events:
                    if model != alias:
                        continue
                    if what == 'renamed':
                        previous = json.loads(detail).get('was')
                        if not isinstance(previous,str) or not previous:
                            continue
                        periods.append((identity,alias,at,upper))
                        alias,upper=previous,at
                    else:
                        periods.append((identity,alias,at,upper))
                        break
                else:
                    periods.append((identity,alias,0,upper))
                self.db.executemany('INSERT INTO model_names VALUES (?,?,?,?) ON CONFLICT(model_id,name,since) DO NOTHING', periods)

    def identity(self, name: str) -> str | None:
        """The stable ID of a current local entry, independent of its label and settings."""
        row = self.db.execute('SELECT identity FROM models WHERE name=?', (name,)).fetchone()
        return row[0] if row else None

    def name_for(self, identity: str) -> str | None:
        """Resolve only a live identity; deleting/recreating a label cannot inherit old work."""
        row = self.db.execute('SELECT name FROM models WHERE identity=?', (identity,)).fetchone()
        return row[0] if row else None

    def name_history(self, name: str) -> list[dict]:
        """Expose the known naming periods for an authorized model's legacy references."""
        return [{'name':alias,'since':since,'until':until} for alias,since,until in self.db.execute(
            'SELECT name,since,until FROM model_names WHERE model_id=? ORDER BY since', (self.identity(name),))]

    def legacy_identity(self, name: str, at: float) -> str | None:
        """Bind a historical label only when its timestamp identifies one model unambiguously."""
        found = {row[0] for row in self.db.execute('SELECT model_id FROM model_names WHERE name=? AND since<=? AND (until IS NULL OR until>?)', (name,at,at))}
        return next(iter(found)) if len(found)==1 else None

    def file_revision(self, name: str) -> int:
        """An explicit selected-file change starts a new tested configuration revision."""
        row = self.db.execute('SELECT file_revision FROM models WHERE name=?',(name,)).fetchone()
        return row[0] if row else 0

    def place(self, name: str) -> int | None:
        """A model's sort order: the one it was given, or FIRST_PLACE. None if there is no such model."""
        row = self.db.execute("SELECT COALESCE(place, ?) FROM models WHERE name = ?", (FIRST_PLACE, name)).fetchone()
        return row and row[0]

    def set_place(self, name: str, place: int | None) -> None:
        """Give a model a sort order, or with None put it back at FIRST_PLACE."""
        check_order(place)
        with self.db:
            self.db.execute("UPDATE models SET place = ? WHERE name = ?", (place, name))

    def setup(self, name: str) -> dict:
        """A complete editable snapshot, separate from inherited flags and runtime state."""
        return {"settings": self.stored(name), "order": self.place(name)}

    def presets(self, name: str) -> list[dict]:
        """Shared settings adapted to this entry's selected weights/projector/adapters."""
        target = self.stored(name)
        primary = self.preset_group(name)['key']
        scopes = dict.fromkeys([primary, shared_presets.engine_scope(target, shared_presets.file_scope(target['flags']['model'])),
                               shared_presets.engine_scope(target, 'entry:' + self.identity(name))])
        result, labels = [], set()
        for scope in scopes:
            for label, settings, updated in self.db.execute(
                    "SELECT name,settings,updated FROM shared_model_presets WHERE scope=? ORDER BY name", (scope,)):
                shown, attempt = label, 0
                while shown in labels:
                    attempt += 1
                    suffix = f' [{scope.split(":", 1)[0]}{" " + str(attempt) if attempt > 1 else ""}]'
                    shown = label[:80-len(suffix)] + suffix
                labels.add(shown)
                result.append({"name": shown, "configuration": shared_presets.for_target(json.loads(settings), target),
                               "updated": updated, "scope": scope, "stored_name": label})
        return result

    def preset_group(self, name: str) -> dict:
        """The file/model family whose presets this live catalogue entry can use."""
        settings, identity = self.stored(name), self.identity(name)
        if settings is None or identity is None:
            raise ValueError("That model no longer exists.")
        described = shared_presets.group(settings['flags'].get('model', ''), identity)
        return {**described, "key": shared_presets.engine_scope(settings, described["key"])}

    def save_preset(self, name: str, label: str, configuration: dict) -> None:
        """Save shared family settings; each target retains its own selected model artifacts."""
        if not isinstance(label, str) or not label.strip() or len(label) > 80 or any(ord(c) < 32 for c in label):
            raise ValueError("A preset name is 1–80 characters, without control characters.")
        scope = self.preset_group(name)['key']
        if previous := next((row for row in self.presets(name) if row['name'] == label.strip()), None):
            scope, label = previous['scope'], previous['stored_name']
        with self.db:
            self.db.execute("""INSERT INTO shared_model_presets VALUES (?,?,?,?)
                ON CONFLICT(scope,name) DO UPDATE SET settings=excluded.settings,updated=excluded.updated""",
                (scope, label.strip(), json.dumps(configuration), time.time()))

    def delete_preset(self, name: str, label: str) -> bool:
        """Delete the selected shared or legacy scope without touching model settings."""
        selected = next((row for row in self.presets(name) if row['name'] == label), None)
        if selected is None:
            return False
        with self.db:
            return bool(self.db.execute("DELETE FROM shared_model_presets WHERE scope=? AND name=?", (selected['scope'], selected['stored_name'])).rowcount)

    def setup_undo(self, name: str) -> dict | None:
        """Read the latest setup receipt and before/after settings for a model."""
        row = self.db.execute("SELECT receipt,before_settings,after_settings,changed FROM model_setup_undo WHERE model_id=?", (self.identity(name),)).fetchone()
        return row and {"receipt": row[0], "before": json.loads(row[1]), "after": json.loads(row[2]), "changed": row[3]}

    def change_setup(self, name: str, configuration: dict, expected: dict, undo: str = "", *, identity: str, revision: int) -> None:
        """Compare-and-swap the settings and undo record in one transaction on either engine.

        This never unloads a server. File selection revisions advance exactly as in put().
        A later ordinary edit cannot be overwritten by an old undo receipt.
        """
        with self.db:
            self.write_setup(name, configuration, expected, undo, identity=identity, revision=revision)

    def write_setup(self, name: str, configuration: dict, expected: dict, undo: str = "", *, identity: str, revision: int) -> None:
        """Write one guarded setup inside the caller's transaction, including its undo receipt.

        Named load-outs use this to commit all members together. Ordinary model setup wraps
        this same operation in its own transaction above.
        """
        row = self.db.execute("SELECT settings,place,identity,file_revision FROM models WHERE name=?", (name,)).fetchone()
        if row is None or row[2:] != (identity, revision) or {"settings": json.loads(row[0]), "order": row[1] if row[1] is not None else FIRST_PLACE} != expected:
            raise ValueError("The model changed. Preview it again.")
        if undo:
            previous = self.setup_undo(name)
            if not previous or previous["receipt"] != undo or previous["after"] != expected or previous["before"] != configuration:
                raise ValueError("That undo is no longer current. Preview it again.")
        revised = int(file_selection(expected["settings"]) != file_selection(configuration["settings"]))
        changed = self.db.execute("""UPDATE models SET settings=?,place=?,file_revision=file_revision+?
            WHERE name=? AND identity=? AND settings=? AND file_revision=? AND COALESCE(place,?)=?""",
            (json.dumps(configuration["settings"]), configuration["order"], revised, name, row[2], row[0], row[3], FIRST_PLACE, expected["order"]))
        if not changed.rowcount:
            raise ValueError("The model changed. Preview it again.")
        if undo:
            self.db.execute("DELETE FROM model_setup_undo WHERE model_id=?", (row[2],))
        else:
            self.db.execute("""INSERT INTO model_setup_undo VALUES (?,?,?,?,?)
                ON CONFLICT(model_id) DO UPDATE SET receipt=excluded.receipt,before_settings=excluded.before_settings,
                after_settings=excluded.after_settings,changed=excluded.changed""",
                (row[2], secrets.token_hex(16), json.dumps(expected), json.dumps(configuration), time.time()))

    def locked(self) -> list[str]:
        """The models that are locked in place."""
        return [name for (name,) in self.db.execute("SELECT name FROM models WHERE locked = 1")]

    def set_locked(self, name: str, on: bool) -> None:
        """Lock a model in place, or let it go."""
        with self.db:
            self.db.execute("UPDATE models SET locked = ? WHERE name = ?", (1 if on else None, name))

    def stored(self, name: str) -> dict | None:
        """A model's settings exactly as they were given, or None if there is no such model."""
        row = self.db.execute("SELECT settings FROM models WHERE name = ?", (name,)).fetchone()
        return row and json.loads(row[0])

    def get(self, name: str) -> dict | None:
        """A model's flags over the defaults; a null override removes an inherited flag."""
        settings = self.stored(name)
        return settings and effective(settings, self.defaults)

    def put(self, name: str, settings: dict) -> None:
        """Add a model, or replace the settings of one that exists. Check them first with check_model."""
        with self.db:
            previous = self.stored(name)
            if previous is not None:
                changed = file_selection(previous) != file_selection(settings)
                self.db.execute('UPDATE models SET settings=?,file_revision=file_revision+? WHERE name=?', (json.dumps(settings),int(changed),name))
            else:
                identity, now = 'model_' + secrets.token_hex(16), time.time()
                self.db.execute('INSERT INTO models(name,settings,identity) VALUES (?,?,?)',(name,json.dumps(settings),identity))
                self.db.execute('INSERT INTO model_identities VALUES (?,?,NULL)',(identity,now))
                self.db.execute('INSERT INTO model_names VALUES (?,?,?,NULL)',(identity,name,now))

    def file_hash(self, path: str) -> str | None:
        """The SHA-256 of a file, as it was worked out before. None if it never was, or if the file has changed since or is gone."""
        try:
            now = os.stat(path)
        except OSError:
            return None
        row = self.db.execute("SELECT size, changed, sha256 FROM file_hashes WHERE path = ?", (path,)).fetchone()
        return row[2] if row and row[:2] == (now.st_size, now.st_mtime) else None

    def keep_hash(self, path: str, sha256: str, size: int, changed: float) -> None:
        """Keep a file's SHA-256, with the size and date the file had when it was read."""
        with self.db:
            self.db.execute("INSERT INTO file_hashes (path, size, changed, sha256) VALUES (?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET size=excluded.size,changed=excluded.changed,sha256=excluded.sha256", (path, size, changed, sha256))

    def unhashed(self) -> list[str]:
        """The files of every model that are there and whose SHA-256 is not known, each once."""
        paths = {path for name in self.names() for path in files_of(self.stored(name)).values()}
        return sorted(path for path in paths if os.path.isfile(path) and self.file_hash(path) is None)

    def fingerprint(self, settings: dict | None, name: str = "") -> str | None:
        """The fingerprint of a model with these settings, as they are used. None while the SHA-256 of one of its files is not known.

        Every setting is in it: each flag, the defaults among them, the cards, `alone`. A file is
        in it by what it holds; selecting another path increments a separate file revision.
        `name` resolves that revision and is written down beside it, never hashed itself.
        """
        if settings is None:
            return None
        selected = engine_for(settings)
        hashes = {role: self.file_hash(path) for role, path in files_of(settings).items()}
        if any(value is None for value in hashes.values()):
            return None
        hashed = selected.model.fingerprint_settings(settings, hashes)
        applied = settings.get("adapters", {})
        settings = base(settings) | ({"adapters": applied} if applied else {})
        if revision := self.file_revision(name):
            hashed['file_revision'] = revision
        mark = hashlib.sha256(json.dumps(hashed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if mark not in self.remembered:
            self.remember(mark, name, settings)
        return mark

    def remember(self, mark: str, name: str, settings: dict) -> None:
        """Write down what a fingerprint stands for, unless that was done before. It stays for ever.

        Kept are the model's name and its settings as they are used, and of each of its files,
        adapters among them, the path, the size, the SHA-256 and what the file says about
        itself. All as they are now, which is when the fingerprint was first seen.
        """
        if self.db.execute("SELECT 1 FROM fingerprints WHERE fingerprint = ?", (mark,)).fetchone():
            self.remembered.add(mark)
            return
        files = {}
        for what, path in files_of(settings).items():
            try:
                says = facts(read_metadata(path))
            except (OSError, ValueError):  # not a .gguf file as far as can be read. The rest is still worth keeping
                says = None
            files[what] = {"path": path, "size": os.stat(path).st_size, "sha256": self.file_hash(path), "says": says}
        with self.db:
            self.db.execute("INSERT INTO fingerprints (fingerprint, first, details) VALUES (?, ?, ?) ON CONFLICT(fingerprint) DO NOTHING",
                            (mark, time.time(), json.dumps({"model": name, "model_id": self.identity(name), "file_revision": self.file_revision(name), "settings": settings, "files": files})))  # fmt: skip
        self.remembered.add(mark)

    def fingerprints(self, start: str = "") -> list[dict]:
        """Every fingerprint that was ever seen and begins with `start`, the latest first: `fingerprint`, `first` and what the model was then."""
        rows = self.db.execute("SELECT fingerprint, first, details FROM fingerprints WHERE fingerprint LIKE ? ORDER BY first DESC, fingerprint DESC", (start.replace("%", "") + "%",))
        return [{"fingerprint": mark, "first": first, **json.loads(details)} for mark, first, details in rows]

    def stamps(self, settings: dict) -> list:
        """The size and date of each file a model uses, or None for one that is gone. A measure is only good for the files it was taken with."""
        return [[os.stat(path).st_size, os.stat(path).st_mtime] if os.path.isfile(path) else None for path in files_of(base(settings)).values()]

    def measurements(self, name: str, settings: dict | None = None) -> list[dict]:
        """All context measurements for these files and settings, including failed calibration loads.

        Context and its lower bound do not invalidate other points on the same curve. Every
        other setting and the files must match. Old single-measure rows are read unchanged.
        Kin share the highest observation at each context, as they share one server.
        """
        now = settings or self.get(name)
        if now is None:
            return []
        found = {}
        for (kept,) in self.db.execute("SELECT measured FROM models WHERE measured IS NOT NULL"):
            rows = json.loads(kept)
            for seen in rows if isinstance(rows, list) else [rows]:
                if seen.get("resident_only") or memory_base(seen["settings"]) != memory_base(now) or seen.get("files") != self.stamps(now):
                    continue
                context = seen["settings"]["flags"]["ctx-size"]
                before = found.get(context)
                point = {"context": context, "mib": seen["mib"], "at": seen["at"], "error": seen.get("error"),**({'image_profile':seen['image_profile']} if seen.get('image_profile') else {})}
                if before and before["mib"] and point["mib"] and len(before["mib"]) == len(point["mib"]):
                    point["mib"] = [max(pair) for pair in zip(before["mib"], point["mib"])]
                    point["at"] = max(before["at"], point["at"])
                elif before and before["at"] > point["at"]:
                    continue
                found[context] = point
        return sorted(found.values(), key=lambda point: point["context"])

    def measure(self, name: str, settings: dict | None = None) -> dict | None:
        """The highest observed MiB at exactly this context; never an estimate from file size."""
        settings = settings or self.get(name)
        if settings is None:
            return None
        return next((point for point in self.measurements(name, settings)
                     if point["context"] == settings["flags"]["ctx-size"] and point["mib"]), None)

    def calibrated(self, name: str, settings: dict | None = None) -> bool:
        """Whether minimum, midpoint and maximum have each been tried for the current setup."""
        settings = settings or self.get(name)
        return bool(settings and set(contexts(settings)) <= {point["context"] for point in self.measurements(name, settings)})

    def measured(self, name: str, settings: dict, mib: list[int] | None, error: str | None = None, *,image_profile=None) -> list[int] | None:
        """Keep a context's peak MiB, or why its isolated calibration load failed.

        Points at other contexts and under older settings stay in the record. Only matching
        files and settings are used for placement or shown as current measurements.
        """
        resident = self.resident_measure(name, settings)
        before = self.measure(name, settings)
        if before and mib and len(before["mib"]) == len(mib):
            mib = [max(pair) for pair in zip(before["mib"], mib)]
        row = self.db.execute("SELECT measured FROM models WHERE name = ?", (name,)).fetchone()
        rows = json.loads(row[0]) if row and row[0] else []
        rows = rows if isinstance(rows, list) else [rows]
        stamps = self.stamps(settings)
        rows = [point for point in rows if not (base(point["settings"]) == base(settings) and point.get("files") == stamps)]
        rows.append({"settings": settings, "files": stamps, "mib": mib, "at": time.time(), "error": error, **({"rss_mib": resident} if resident is not None else {}),**({'image_profile':image_profile} if image_profile else {})})
        with self.db:
            self.db.execute("UPDATE models SET measured = ? WHERE name = ?", (json.dumps(rows), name))
        return mib

    def resident_measure(self, name: str, settings: dict, mib: int | None = None) -> int | None:
        """Persist peak resident RAM alongside context measurements, with the same invalidation.

        GPU memory curves remain separate: resident RAM is measured on CPU and GPU servers.
        """
        rows = []
        for (kept,) in self.db.execute("SELECT measured FROM models WHERE measured IS NOT NULL"):
            points = json.loads(kept)
            rows.extend(points if isinstance(points, list) else [points])
        stamps = self.stamps(settings)
        matching = [point for point in rows if base(point['settings']) == base(settings)
                    and point.get('files') == stamps and point.get('rss_mib') is not None]
        peak = max([point['rss_mib'] for point in matching] + ([mib] if mib is not None else []), default=None)
        if mib is not None and peak is not None and self.stored(name):
            row = self.db.execute("SELECT measured FROM models WHERE name = ?", (name,)).fetchone()
            own = json.loads(row[0]) if row and row[0] else []
            own = own if isinstance(own, list) else [own]
            point = next((point for point in own if base(point['settings']) == base(settings) and point.get('files') == stamps), None)
            if point is None:
                point = {'settings': settings, 'files': stamps, 'mib': None, 'at': time.time(), 'error': None, 'resident_only': True}
                own.append(point)
            point['rss_mib'] = peak
            with self.db:
                self.db.execute("UPDATE models SET measured = ? WHERE name = ?", (json.dumps(own), name))
        return peak

    def run_begun(self, by: str, caller: str, since: str) -> None:
        """Write down that a run measuring the models is under way, and who asked for it."""
        with self.db:
            self.db.execute("DELETE FROM measuring_run")
            self.db.execute("INSERT INTO measuring_run (by, caller, since) VALUES (?, ?, ?)", (by, caller, since))

    def run_ended(self) -> None:
        """Clear the durable measuring-run marker after execution ends."""
        with self.db:
            self.db.execute("DELETE FROM measuring_run")

    def run_under_way(self) -> dict | None:
        """The measuring run that was under way when LoadCoach last stopped, or None. One that ended, in whatever way, is not written down any more."""
        row = self.db.execute("SELECT by, caller, since FROM measuring_run").fetchone()
        return row and {"by": row[0], "caller": row[1], "since": row[2]}

    def kin_of(self, settings: dict) -> list[str]:
        """The kin of a model with these settings: the models that differ from it in nothing but their adapters and their projector. In the order models are listed: see names()."""
        return [name for name in self.names() if kin_base(self.get(name)) == kin_base(settings)]

    def kin(self, name: str) -> list[str]:
        """A model and its kin."""
        return self.kin_of(self.get(name) or {})

    def forget_measure(self, name: str) -> bool:
        """Throw away what a model and its kin measured, so that they are measured afresh at their next load. False if there was nothing."""
        with self.db:
            forgot = [self.db.execute("UPDATE models SET measured = NULL WHERE name = ? AND measured IS NOT NULL", (one,)).rowcount for one in self.names() if memory_base(self.get(one)) == memory_base(self.get(name) or {"flags": {}})]
        return any(forgot)

    def rename(self, name: str, new: str) -> None:
        """Give a model another name. Its settings and its sort order stay. Raises ValueError, saying why, if it cannot be done."""
        if not NAME.fullmatch(new):
            raise ValueError(f"{new!r}: a model's name is letters, digits, dots, dashes, colons or slashes")
        if self.stored(name) is None:
            raise ValueError(f"There is no model named {name!r}.")
        if self.stored(new) is not None:
            raise ValueError(f"There is already a model named {new!r}.")
        with self.db:
            identity, now = self.identity(name), time.time()
            self.db.execute("UPDATE models SET name = ? WHERE name = ?", (new, name))
            self.db.execute('UPDATE model_names SET until=? WHERE model_id=? AND until IS NULL',(now,identity))
            self.db.execute('INSERT INTO model_names VALUES (?,?,?,NULL)',(identity,new,now))

    def remove(self, name: str) -> bool:
        """Take a model off the list. False if it was not on it. Its file is left where it is."""
        with self.db:
            identity, now = self.identity(name), time.time()
            if identity is None:
                return False
            self.db.execute('UPDATE model_identities SET removed=? WHERE id=?',(now,identity))
            self.db.execute('UPDATE model_names SET until=? WHERE model_id=? AND until IS NULL',(now,identity))
            self.db.execute('DELETE FROM model_presets WHERE model_id=?', (identity,))
            self.db.execute('DELETE FROM shared_model_presets WHERE scope=?',
                            (shared_presets.engine_scope(self.stored(name), 'entry:' + identity),))
            self.db.execute('DELETE FROM model_setup_undo WHERE model_id=?', (identity,))
            return self.db.execute("DELETE FROM models WHERE name = ?", (name,)).rowcount > 0
