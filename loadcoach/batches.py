"""Batches: requests handed in to be answered later, as OpenAI's Batch API has them.

A program uploads a file with one request to a line, starts a batch from it, and comes back
for the answers. Everything is written down the moment it happens, so a batch outlives
LoadCoach being restarted, compute going to sleep, or a crash: at the next start it goes on
with the requests that have no answer yet.

They are kept in the same file as the users. This file only keeps them. Answering the
requests is in app.py, where each goes through the line like any other request.

    files         what was uploaded, and the answers once a batch has ended
    batches       one row for each batch: whose, its state, when
    batch_lines   one row for each request of a batch, and its answer once it has one
"""

from __future__ import annotations

import email
import email.policy
import json
import secrets
import time

from db import DatabaseSource, database, migrate
from db.schemas import BATCHES

ENDPOINTS = ("/v1/chat/completions", "/v1/completions", "/v1/embeddings", "/v1/images/generations", "/v1/images/edits", "/v1/audio/generations")
MOST_LINES = 10_000  # requests in one batch
MOST_BYTES = 50 * 2**20  # the size of one uploaded file
TRIES = 3  # how often one request is begun. Each time it is cut off by LoadCoach going down counts, so that one which brings it down is not begun for ever
OPEN = ("in_progress", "cancelling")  # the states of a batch that has not ended
AGAIN_AFTER = 30  # seconds before a request that was turned away for now is tried again


class NoLocalBatches:
    """A node that keeps no batch or task records: its store was retired after the DB1 cutover, and the front keeps them."""
    db = None

    def bind_models(self, models):
        """Leave model binding to the front, which keeps the batches."""
        pass

    def get(self, number):
        """Return no local batch: the front keeps batch records."""
        return None

    def listing(self, user=None):
        """Return an empty local batch list: the front keeps batch records."""
        return []

    def unfinished(self):
        """Return no locally resumable batches: the front keeps batch records."""
        return []

    def open(self):
        """Report zero local open batches: the front keeps batch records."""
        return 0

    def prune(self, days):
        """Leave batch retention to the front, which keeps the batches."""
        pass


def uploaded(content_type: str, body: bytes) -> dict[str, tuple[str, bytes]]:
    """The fields of a form that was sent as multipart/form-data, which is how a file is uploaded: each by its name, with its file name, if it has one, and its bytes.

    Such a body is laid out as an e-mail with attachments is, so Python's own reader of those
    takes it apart. Raises ValueError if it is not such a form.
    """
    if not content_type.lower().startswith("multipart/form-data"):
        raise ValueError("A file is uploaded as a form, multipart/form-data.")
    message = email.message_from_bytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body, policy=email.policy.HTTP)
    return {part.get_param("name", header="content-disposition"): (part.get_filename() or "", part.get_payload(decode=True)) for part in message.iter_parts()}


def lines_of(content: bytes, endpoint: str) -> list[dict]:
    """The requests in an uploaded file, each as `custom_id` and `body`. Raises ValueError, naming the line, if the file is not a batch for this endpoint."""
    found, seen = [], set()
    for number, text in enumerate(content.decode("utf-8", "replace").splitlines(), 1):
        if not text.strip():
            continue
        try:
            one = json.loads(text)
            good = isinstance(one["custom_id"], str) and one["custom_id"] not in seen and isinstance(one["body"], dict) and isinstance(one["body"].get("model"), str)
        except (ValueError, KeyError, TypeError):
            good = False
        if not good or one.get("method", "POST") != "POST" or one.get("url") != endpoint:
            raise ValueError(f'Line {number} is not a request for this batch. Each line is JSON with a "custom_id" of its own, "method": "POST", "url": "{endpoint}" and a "body" that names a model.')
        seen.add(one["custom_id"])
        found.append({"custom_id": one["custom_id"], "body": one["body"]})
    if not found or len(found) > MOST_LINES:
        raise ValueError(f"A batch has from 1 to {MOST_LINES} requests. This file has {len(found)}.")
    return found


class Batches:
    """The uploaded files, the batches and their requests. Every change is written at once."""

    def __init__(self, path: DatabaseSource) -> None:
        """Open durable batch and upload storage and apply its portable migrations."""
        source = database(path)
        self.db = source.connect(check_same_thread=False)
        migrate(source, self.db, "loadcoach.batches", BATCHES)
        self.models = None

    # ---- files -----------------------------------------------------------------------------
    def bind_models(self, models) -> None:
        """Pin queued local requests by identity, preserving their original uploaded bodies."""
        self.models = models
        with self.db:
            for number, line, body, created in self.db.execute('SELECT l.batch,l.line,l.body,b.created FROM batch_lines l JOIN batches b ON b.id=l.batch WHERE l.model_id IS NULL').fetchall():
                identity = models.legacy_identity(json.loads(body).get('model'),created)
                if identity:
                    self.db.execute('UPDATE batch_lines SET model_id=? WHERE batch=? AND line=?',(identity,number,line))

    def put_file(self, user: str, filename: str, purpose: str, content: bytes) -> dict:
        """Keep a file, and return it as OpenAI describes one."""
        number = "file-" + secrets.token_hex(12)
        with self.db:
            self.db.execute("INSERT INTO files (id, user, filename, purpose, created, content) VALUES (?, ?, ?, ?, ?, ?)", (number, user, filename, purpose, time.time(), content))
        return self.file(number)  # type: ignore[return-value]

    def file(self, number: str) -> dict | None:
        """One file as OpenAI describes it, with `user`, whose it is. Not its content. None if there is no such file."""
        row = self.db.execute("SELECT user, filename, purpose, created, length(content) FROM files WHERE id = ?", (number,)).fetchone()
        return row and {"id": number, "object": "file", "bytes": row[4], "created_at": int(row[3]), "filename": row[1], "purpose": row[2], "user": row[0]}

    def content(self, number: str) -> bytes | None:
        """Read a retained upload's bytes, or None when it does not exist."""
        row = self.db.execute("SELECT content FROM files WHERE id = ?", (number,)).fetchone()
        return row and row[0]

    def files(self, user: str) -> list[dict]:
        """One user's files, the latest first."""
        return [self.file(number) for (number,) in self.db.execute("SELECT id FROM files WHERE user = ? ORDER BY created DESC", (user,))]  # type: ignore[misc]

    def remove_file(self, number: str) -> None:
        """Delete the selected retained batch upload."""
        with self.db:
            self.db.execute("DELETE FROM files WHERE id = ?", (number,))

    # ---- batches ---------------------------------------------------------------------------

    def start(self, user: str, endpoint: str, input_file: str, lines: list[dict], metadata: dict | None = None, *, number: str | None = None) -> dict:
        """Write a batch down with every one of its requests. From here on it goes on until it has ended, whatever happens to LoadCoach."""
        number = number or "batch_" + secrets.token_hex(12)
        with self.db:
            self.db.execute("INSERT INTO batches (id, user, endpoint, input_file, status, created, metadata) VALUES (?, ?, ?, ?, 'in_progress', ?, ?)",
                            (number, user, endpoint, input_file, time.time(), json.dumps(metadata or {})))  # fmt: skip
            self.db.executemany("INSERT INTO batch_lines (batch, line, custom_id, body, model_id) VALUES (?, ?, ?, ?, ?)",
                                [(number, count, line["custom_id"], json.dumps(line["body"]), self.models.identity(line['body']['model']) if self.models else None) for count, line in enumerate(lines)])  # fmt: skip
        return self.get(number)  # type: ignore[return-value]

    def get(self, number: str) -> dict | None:
        """One batch as OpenAI describes it, with `user`, whose it is. None if there is no such batch."""
        row = self.db.execute("SELECT user, endpoint, input_file, output_file, error_file, status, created, ended, metadata FROM batches WHERE id = ?", (number,)).fetchone()
        if row is None:
            return None
        user, endpoint, input_file, output_file, error_file, status, created, ended, metadata = row
        # A request counts as failed if it was answered with an error, and as completed if it was answered at all otherwise.
        status_value = "json_extract(result, '$.status_code')" if self.db.engine == "sqlite" else "CAST((result::jsonb ->> 'status_code') AS INTEGER)"
        total, answered, failed = self.db.execute(
            f"SELECT count(*), count(result), coalesce(sum(CASE WHEN {status_value} >= 400 THEN 1 ELSE 0 END), 0) FROM batch_lines WHERE batch = ?", (number,)
        ).fetchone()
        when = lambda state: int(ended) if ended and status == state else None  # noqa: E731
        return {
            "id": number, "object": "batch", "endpoint": endpoint, "errors": None, "input_file_id": input_file, "completion_window": "24h", "status": status,
            "output_file_id": output_file, "error_file_id": error_file, "created_at": int(created), "in_progress_at": int(created),
            "completed_at": when("completed"), "cancelled_at": when("cancelled"),
            "request_counts": {"total": total, "completed": answered - failed, "failed": failed},
            "metadata": json.loads(metadata), "user": user,
        }  # fmt: skip

    def listing(self, user: str | None = None) -> list[dict]:
        """Every batch, the latest first. With `user`, only theirs."""
        found = self.db.execute("SELECT id FROM batches WHERE user = ? OR CAST(? AS TEXT) IS NULL ORDER BY created DESC", (user, user))
        return [self.get(number) for (number,) in found]  # type: ignore[misc]

    def unfinished(self) -> list[str]:
        """The batches that have not ended, the oldest first. They are the ones to go on with when LoadCoach starts."""
        marks = ", ".join("?" for _ in OPEN)
        return [number for (number,) in self.db.execute(f"SELECT id FROM batches WHERE status IN ({marks}) ORDER BY created", OPEN)]

    def next_line(self, number: str, *, on_failure=None) -> dict | None:
        """The next request of a batch that has no answer, counted as begun once more. None when every request has one.

        A request that was begun TRIES times and never answered is given an answer here, which
        says so. It was cut off each time, by LoadCoach going down, and is not begun again.
        ``on_failure`` receives that saved failure for the worker's metadata history.
        """
        while True:
            row = self.db.execute("SELECT line, custom_id, body, tries, model_id FROM batch_lines WHERE batch = ? AND result IS NULL ORDER BY line LIMIT 1", (number,)).fetchone()
            if row is None:
                return None
            line, custom_id, body, tries, identity = row
            if tries >= TRIES:
                result = {"error": {"message": f"This request was begun {tries} times and cut off each time, by LoadCoach stopping. It was not begun again.", "code": "cut_off"}}
                self.keep(number, line, 500, "", result)
                if on_failure:
                    on_failure({'line': line, 'custom_id': custom_id, 'body': json.loads(body)}, 500, '', result)
                continue
            with self.db:
                self.db.execute("UPDATE batch_lines SET tries = tries + 1 WHERE batch = ? AND line = ?", (number, line))
            return {"line": line, "custom_id": custom_id, "body": json.loads(body), **({'model_id':identity} if identity else {})}

    def not_begun(self, number: str, line: int) -> None:
        """Take back that a request was begun: it was turned away for now, and is to be tried again later."""
        with self.db:
            self.db.execute("UPDATE batch_lines SET tries = tries - 1 WHERE batch = ? AND line = ?", (number, line))

    def keep(self, number: str, line: int, status_code: int, request_id: str, body) -> None:
        """Keep the answer to one request of a batch."""
        with self.db:
            self.db.execute("UPDATE batch_lines SET result = ? WHERE batch = ? AND line = ?",
                            (json.dumps({"status_code": status_code, "request_id": request_id, "body": body}), number, line))  # fmt: skip

    def cancelling(self, number: str) -> None:
        """Mark an in-progress batch for cooperative cancellation."""
        with self.db:
            self.db.execute("UPDATE batches SET status = 'cancelling' WHERE id = ? AND status IN ('in_progress','paused')", (number,))

    def finish(self, number: str) -> dict:
        """End a batch: write its answers out as files, as OpenAI has them, and say how it ended.

        Requests that were answered well go into the output file, and those answered with an
        error into the error file. One that was never answered, in a batch that was cancelled,
        is in neither.
        """
        batch = self.get(number)
        good, bad = [], []
        for line, custom_id, result in self.db.execute("SELECT line, custom_id, result FROM batch_lines WHERE batch = ? AND result IS NOT NULL ORDER BY line", (number,)):
            answer = json.loads(result)
            failed = answer["status_code"] >= 400
            error = answer["body"].get("error") if failed and isinstance(answer["body"], dict) else None
            (bad if failed else good).append(json.dumps({"id": f"batch_req_{number[6:]}_{line}", "custom_id": custom_id, "response": answer, "error": error}))
        output = self.put_file(batch["user"], f"{number}_output.jsonl", "batch_output", "\n".join(good).encode())["id"] if good else None
        errors = self.put_file(batch["user"], f"{number}_error.jsonl", "batch_output", "\n".join(bad).encode())["id"] if bad else None
        with self.db:
            self.db.execute("UPDATE batches SET status = ?, ended = ?, output_file = ?, error_file = ? WHERE id = ?",
                            ("cancelled" if batch["status"] == "cancelling" else "completed", time.time(), output, errors, number))  # fmt: skip
        return self.get(number)  # type: ignore[return-value]

    def open(self) -> int:
        """How many batches have not ended."""
        return len(self.unfinished())

    def prune(self, days: float) -> None:
        """Remove the files and the requests of batches that ended more than `days` days ago. The batches themselves stay listed. 0 keeps everything."""
        if not days:
            return
        old = self.db.execute("SELECT id, input_file, output_file, error_file FROM batches WHERE ended < ?", (time.time() - days * 86400,)).fetchall()
        with self.db:
            for number, *files in old:
                self.db.executemany("DELETE FROM files WHERE id = ?", [(file,) for file in files if file])
                for line, result in self.db.execute("SELECT line,result FROM batch_lines WHERE batch=?", (number,)).fetchall():
                    compact = None if result is None else json.dumps({"status_code": json.loads(result)["status_code"]})
                    self.db.execute("UPDATE batch_lines SET body='{}', result=? WHERE batch=? AND line=?", (compact, number, line))
