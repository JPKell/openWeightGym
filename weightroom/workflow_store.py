"""Private immutable prompt versions and resumable fixed workflows in the chat store."""

import hashlib
import json
import time

from db import database, migrate
from db.schemas import CHAT
from .knowledge_store import identifier

BUILTINS = {
    "research": (
        "Built-in research",
        "Write a concise factual research note answering the question from the supplied excerpts. Identify gaps and do not invent facts.",
    ),
    "draft": (
        "Built-in draft",
        "Write a coherent, readable draft that answers the original question using the accepted research and supplied excerpts. Preserve factual uncertainty and cite the supplied sources.",
    ),
}


def digest(body):
    """Return the SHA-256 digest of the canonical input."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def builtin(kind):
    """Return the immutable built-in research or draft prompt and its digest."""
    name, body = BUILTINS[kind]
    return dict(
        id="builtin:" + kind + "-v1",
        kind=kind,
        name=name,
        version=1,
        body=body,
        digest=digest(body),
    )


class Workflows:
    """Owner-private prompt versions and durable workflow attempts."""

    def __init__(self, path):
        """Open private workflow storage and apply chat schema migrations."""
        source = database(path)
        self.db = source.connect(check_same_thread=False)
        migrate(source, self.db, "weightroom.chat", CHAT)

    def prompts(self, owner):
        """List immutable private prompt versions owned by the caller."""
        columns = ("id", "name", "kind", "version", "body", "digest", "created")
        return [
            dict(zip(columns, row))
            for row in self.db.execute(
                "SELECT id,name,kind,version,body,digest,created FROM prompt_versions WHERE owner=? ORDER BY created DESC,id",
                (owner,),
            ).fetchall()
        ]

    def prompt(self, owner, number, kind):
        """Resolve a matching built-in or owner-private prompt version."""
        if number == builtin(kind)["id"]:
            return builtin(kind)
        return next(
            (p for p in self.prompts(owner) if p["id"] == number and p["kind"] == kind),
            None,
        )

    def version(self, owner, name, kind, body):
        """Save a new immutable private prompt version within the owner's count limit."""
        number = identifier()
        with self.db:
            if (
                self.db.execute(
                    "SELECT COUNT(*) FROM prompt_versions WHERE owner=?", (owner,)
                ).fetchone()[0]
                >= 200
            ):
                raise ValueError("Keep at most 200 private prompt versions.")
            version = self.db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM prompt_versions WHERE owner=? AND name=? AND kind=?",
                (owner, name, kind),
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO prompt_versions (id,owner,name,kind,version,body,digest,created) VALUES (?,?,?,?,?,?,?,?)",
                (number, owner, name, kind, version, body, digest(body), time.time()),
            )
        return number

    def one(self, owner, number):
        """Read and decode a workflow only when its owner matches the caller."""
        columns = (
            "id",
            "collection",
            "title",
            "question",
            "minimum",
            "revision",
            "level",
            "config",
            "sources",
            "research",
            "draft",
            "state",
            "error",
            "epoch",
            "created",
            "updated",
        )
        row = self.db.execute(
            "SELECT " + ",".join(columns) + " FROM workflows WHERE id=? AND owner=?",
            (number, owner),
        ).fetchone()
        if not row:
            return None
        result = dict(zip(columns, row))
        for key in ("config", "sources", "research", "draft"):
            result[key] = json.loads(result[key])
        return result

    def listing(self, owner):
        """List an owner's workflows with the most recently updated first."""
        return [
            self.one(owner, row[0])
            for row in self.db.execute(
                "SELECT id FROM workflows WHERE owner=? ORDER BY updated DESC,id",
                (owner,),
            ).fetchall()
        ]

    def create(self, owner, title, question, minimum, collection, config):
        """Create a private workflow with pinned collection, models and prompt versions."""
        number = identifier()
        now = time.time()
        with self.db:
            if (
                self.db.execute(
                    "SELECT COUNT(*) FROM workflows WHERE owner=?", (owner,)
                ).fetchone()[0]
                >= 50
            ):
                raise ValueError(
                    "Keep at most fifty workflows; delete one before starting another."
                )
            self.db.execute(
                "INSERT INTO workflows (id,owner,collection,title,question,minimum,revision,level,config,state,created,updated) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    number,
                    owner,
                    collection["id"],
                    title,
                    question,
                    minimum,
                    collection["revision"],
                    collection["level"],
                    json.dumps(config),
                    "ready_research",
                    now,
                    now,
                ),
            )
        return number

    def attempts(self, owner, number):
        """List decoded execution attempts for an owner-private workflow."""
        if not self.one(owner, number):
            return []
        columns = (
            "id",
            "step",
            "ordinal",
            "epoch",
            "state",
            "receipts",
            "usage",
            "output",
            "error",
            "started",
            "ended",
        )
        result = []
        for row in self.db.execute(
            "SELECT "
            + ",".join(columns)
            + " FROM workflow_attempts WHERE workflow=? ORDER BY started,id",
            (number,),
        ).fetchall():
            value = dict(zip(columns, row))
            for key in ("receipts", "usage", "output"):
                value[key] = json.loads(value[key])
            result.append(value)
        return result

    def begin(self, owner, number):
        """Atomically claim the next runnable step and allocate its new attempt epoch."""
        with self.db:
            self.db.execute(
                "UPDATE workflows SET epoch=epoch WHERE id=? AND owner=?",
                (number, owner),
            )
            run = self.one(owner, number)
            if not run:
                return None
            step = next(
                (
                    s
                    for s in BUILTINS
                    if run["state"] in ("ready_" + s, "paused_" + s, "failed_" + s)
                ),
                None,
            )
            if not step:
                raise ValueError("This workflow is not ready to run a step.")
            ordinal = self.db.execute(
                "SELECT COUNT(*)+1 FROM workflow_attempts WHERE workflow=? AND step=?",
                (number, step),
            ).fetchone()[0]
            if ordinal > 10:
                raise ValueError(
                    "This step has reached ten attempts. Create a new workflow to continue."
                )
            epoch = run["epoch"] + 1
            attempt = identifier()
            self.db.execute(
                "UPDATE workflows SET state=?,epoch=?,error=?,updated=? WHERE id=? AND owner=?",
                (step + "ing", epoch, "", time.time(), number, owner),
            )
            self.db.execute(
                "INSERT INTO workflow_attempts (id,workflow,step,ordinal,epoch,state,started) VALUES (?,?,?,?,?,?,?)",
                (attempt, number, step, ordinal, epoch, "running", time.time()),
            )
        return dict(id=attempt, step=step, epoch=epoch, ordinal=ordinal)

    def sources(self, owner, number, epoch, sources):
        """Retain source references only while the workflow epoch still matches."""
        references = [
            {
                k: s[k]
                for k in (
                    "id",
                    "document",
                    "digest",
                    "generation",
                    "name",
                    "page",
                    "line",
                )
            }
            for s in sources
        ]
        with self.db:
            return (
                self.db.execute(
                    "UPDATE workflows SET sources=? WHERE id=? AND owner=? AND epoch=?",
                    (json.dumps(references), number, owner, epoch),
                ).rowcount
                == 1
            )

    def receipt(self, owner, number, attempt, receipt):
        """Append a deduplicated request receipt to the selected private attempt."""
        if not self.one(owner, number):
            return
        with self.db:
            row = self.db.execute(
                "SELECT receipts FROM workflow_attempts WHERE id=? AND workflow=?",
                (attempt, number),
            ).fetchone()
            if row:
                receipts = json.loads(row[0])
                if not any(r.get("request") == receipt["request"] for r in receipts):
                    receipts.append(receipt)
                self.db.execute(
                    "UPDATE workflow_attempts SET receipts=? WHERE id=?",
                    (json.dumps(receipts), attempt),
                )

    def usage(self, owner, number, attempt, usage):
        """Reconcile matching receipts while preserving previously known usage and cost."""
        if self.one(owner, number):
            with self.db:
                row = self.db.execute(
                    "SELECT receipts,usage,step FROM workflow_attempts WHERE id=? AND workflow=?",
                    (attempt, number),
                ).fetchone()
                if not row:
                    return
                if [r["request"] for r in json.loads(row[0])] != [
                    r["request"] for r in usage["requests"]
                ]:
                    return
                previous = {
                    r["request"]: r for r in json.loads(row[1]).get("requests", [])
                }
                for record in usage["requests"]:
                    old = previous.get(record["request"], {})
                    for key in ("cost", "prompt_tokens", "output_tokens"):
                        if record[key] is None:
                            record[key] = old.get(key)
                records = usage["requests"]
                has_model_receipt = any(
                    r.get("stage") == row[2] for r in json.loads(row[0])
                )
                for field in ("cost", "prompt_tokens", "output_tokens"):
                    usage[field] = (
                        sum(r[field] for r in records)
                        if has_model_receipt
                        and records
                        and all(r[field] is not None for r in records)
                        else None
                    )
                self.db.execute(
                    "UPDATE workflow_attempts SET usage=? WHERE id=? AND workflow=?",
                    (json.dumps(usage, allow_nan=False), attempt, number),
                )

    def finish(self, owner, number, attempt, state, output=None, error=""):
        """Accept output only for the current running attempt epoch."""
        step = attempt["step"]
        now = time.time()
        output = output or {}
        with self.db:
            changed = self.db.execute(
                "UPDATE workflows SET state=?,error=?,updated=? WHERE id=? AND owner=? AND epoch=? AND state=?",
                (state, error, now, number, owner, attempt["epoch"], step + "ing"),
            ).rowcount
            if not changed:
                return False
            if state == "review_" + step:
                self.db.execute(
                    f"UPDATE workflows SET {step}=? WHERE id=?",
                    (json.dumps(output), number),
                )  # step comes only from the two fixed kinds
            self.db.execute(
                "UPDATE workflow_attempts SET state=?,output=?,error=?,ended=? WHERE id=? AND workflow=?",
                (state, json.dumps(output), error, now, attempt["id"], number),
            )
        return True

    def accept(self, owner, number, step):
        """Advance reviewed non-abstaining output to the next workflow state."""
        if not isinstance(step, str) or step not in BUILTINS:
            raise ValueError("Choose research or draft.")
        state = "ready_draft" if step == "research" else "completed"
        with self.db:
            self.db.execute(
                "UPDATE workflows SET epoch=epoch WHERE id=? AND owner=?",
                (number, owner),
            )
            run = self.one(owner, number)
            if not run or run["state"] != "review_" + step:
                raise ValueError("This output is not awaiting acceptance.")
            if not run[step] or run[step].get("abstain"):
                raise ValueError(
                    "An abstention cannot be accepted as research or a draft."
                )
            self.db.execute(
                "UPDATE workflows SET state=?,updated=? WHERE id=? AND owner=?",
                (state, time.time(), number, owner),
            )

    def pause(self, owner, number):
        """Invalidate the running epoch and mark its attempt cancelled."""
        with self.db:
            self.db.execute(
                "UPDATE workflows SET epoch=epoch WHERE id=? AND owner=?",
                (number, owner),
            )
            run = self.one(owner, number)
            if not run:
                return
            step = next((s for s in BUILTINS if run["state"] == s + "ing"), None)
            if step:
                self.db.execute(
                    "UPDATE workflows SET state=?,epoch=epoch+1,error=?,updated=? WHERE id=?",
                    (
                        "paused_" + step,
                        "Cancelled; resume explicitly.",
                        time.time(),
                        number,
                    ),
                )
                self.db.execute(
                    "UPDATE workflow_attempts SET state='cancelled',error='Cancelled; no output accepted.',ended=? WHERE workflow=? AND epoch=? AND state='running'",
                    (time.time(), number, run["epoch"]),
                )

    def interrupted(self):
        """Pause unfinished steps after restart without replaying model requests."""
        with self.db:
            for step in BUILTINS:
                self.db.execute(
                    "UPDATE workflows SET state=?,epoch=epoch+1,error=?,updated=? WHERE state=?",
                    (
                        "paused_" + step,
                        "WeightRoom stopped; resume explicitly with current authorization.",
                        time.time(),
                        step + "ing",
                    ),
                )
            self.db.execute(
                "UPDATE workflow_attempts SET state='interrupted',error='WeightRoom stopped before this attempt finished.',ended=? WHERE state='running'",
                (time.time(),),
            )

    def remove(self, owner, number):
        """Delete an owner's workflow and all its execution attempts."""
        with self.db:
            if self.one(owner, number):
                self.db.execute(
                    "DELETE FROM workflow_attempts WHERE workflow=?", (number,)
                )
                self.db.execute(
                    "DELETE FROM workflows WHERE id=? AND owner=?", (number, owner)
                )

    def forget(self, owner):
        """Remove the owner's workflows and immutable private prompt versions."""
        for run in self.listing(owner):
            self.remove(owner, run["id"])
        with self.db:
            self.db.execute("DELETE FROM prompt_versions WHERE owner=?", (owner,))
