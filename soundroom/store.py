"""Private audio files, kept on the front, and metadata, using the existing accounts store.

The batch executor remains openWeightGym's existing front executor; this store binds its audio
jobs to immutable account IDs and keeps audio bytes out of the database. An owner may
share a take, sound or voice with groups; only the owner may change or delete it.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import time

from db import migrate
from soundroom.schema import SOUNDROOM

ASSET_ID = re.compile(r"audio_[a-f0-9]{32}")
KINDS = ("take", "sound", "speech", "reference", "recording")
UPLOAD_BYTES = 32 * 2**20
OUTPUT_BYTES = 256 * 2**20
TAG = re.compile(r"[\w][\w .+#&'-]{0,39}")
PREFERENCES = {"format": ("wav", "flac", "mp3", "opus"), "voice": None, "speech_model": None, "transcription_model": None, 'autoplay': False}


def probe(content, most_seconds=3600):
    """Read duration, rate and channels with ffprobe; anything it cannot read is refused."""
    with tempfile.NamedTemporaryFile(prefix="suite-audio-probe-") as file:
        file.write(content)
        file.flush()
        try:
            answer = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                                     "stream=sample_rate,channels:format=duration", "-of", "json", file.name],
                                    capture_output=True, text=True, timeout=30)
            found = json.loads(answer.stdout or "{}")
            stream, seconds = found["streams"][0], float(found["format"]["duration"])
            rate, channels = int(stream["sample_rate"]), int(stream["channels"])
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError):
            raise ValueError("That is not an audio file this server can read.") from None
    if not 0 < seconds <= most_seconds or not 1 <= channels <= 8 or not 1000 <= rate <= 384000:
        raise ValueError(f"Audio must be up to {most_seconds} seconds with 1–8 channels.")
    return seconds, rate, channels


class Store:
    """Audio history and assets scoped to stable owner IDs; temporary assets never touch disk or SQL."""

    def __init__(self, connection, root):
        """Initialize private audio state."""
        self.db = connection
        migrate(connection.database, connection, "soundroom.front", SOUNDROOM)
        self.root = Path(root) / "soundroom-assets"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.temporary = {}

    def path(self, identifier):
        """Map generated IDs only to private files; caller filenames never become paths."""
        if not isinstance(identifier, str) or not ASSET_ID.fullmatch(identifier):
            raise ValueError("Invalid audio asset ID.")
        return self.root / "assets" / identifier[6:8] / (identifier + ".audio")

    def expire(self):
        """Bound temporary no-log assets by age."""
        now = time.monotonic()
        for key in [key for key, value in self.temporary.items() if value["until"] < now]:
            self.temporary.pop(key)

    def save(self, owner, content, *, kind="reference", filename="audio.wav", metadata=None, temporary=False, job=None):
        """Atomically retain one validated asset, or hold it only in bounded temporary memory."""
        if not owner or kind not in KINDS:
            raise ValueError("Invalid audio owner or kind.")
        output = kind in ("take", "sound", "speech")
        if not isinstance(content, bytes) or not content or len(content) > (OUTPUT_BYTES if output else UPLOAD_BYTES):
            raise ValueError("Audio exceeds its upload or output size limit.")
        seconds, rate, channels = probe(content)
        identifier = "audio_" + secrets.token_hex(16)
        filename = Path(str(filename).replace("\\", "/")).name[:160] or "audio.wav"
        record = {"id": identifier, "kind": kind, "filename": filename, "sha256": hashlib.sha256(content).hexdigest(),
                  "seconds": round(seconds, 3), "rate": rate, "channels": channels, "bytes": len(content), "created": time.time(),
                  "metadata": metadata or {}, "temporary": temporary, "favourite": False, "tags": [], "mine": True, "shared_with": []}
        if temporary:
            self.expire()
            if sum(len(row["content"]) for row in self.temporary.values()) + len(content) > 512 * 2**20:
                raise ValueError("Temporary audio memory is full; remove temporary audio and retry.")
            self.temporary[identifier] = {"owner": owner, "record": record, "content": content, "until": time.monotonic() + 3600}
            return record
        path = self.path(identifier)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if shutil.disk_usage(self.root).free < len(content) + 2**30:
            raise ValueError("Front audio storage needs at least 1 GiB free headroom.")
        try:
            with path.open("xb") as stream:
                os.chmod(path, 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            with self.db:
                self.db.execute("INSERT INTO soundroom_assets (id,owner_id,kind,filename,sha256,seconds,rate,channels,bytes,created,metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (identifier, owner, kind, filename, record["sha256"], record["seconds"], rate, channels, len(content), record["created"], json.dumps(metadata or {})))
                if job:
                    self.db.execute("INSERT INTO soundroom_job_assets VALUES (?,?,?)", (job, identifier, "output"))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return record

    def shared_with(self, item):
        """The groups an owner shared an item with."""
        return [group for group, in self.db.execute("SELECT group_name FROM soundroom_shares WHERE item=? ORDER BY group_name", (item,)).fetchall()]

    def asset(self, owner, identifier, group=None):
        """Read the caller's asset, or one shared with the caller's group; absent and foreign look the same."""
        self.expire()
        temporary = self.temporary.get(identifier)
        if temporary:
            return dict(temporary["record"]) if temporary["owner"] == owner else None
        row = self.db.execute("SELECT owner_id,kind,filename,sha256,seconds,rate,channels,bytes,created,metadata,favourite,tags FROM soundroom_assets WHERE id=?", (identifier,)).fetchone()
        if not row:
            return None
        shared = self.shared_with(identifier)
        if row[0] != owner and (group is None or group not in shared):
            return None
        return {"id": identifier, **dict(zip(("kind", "filename", "sha256", "seconds", "rate", "channels", "bytes", "created"), row[1:9])),
                "metadata": json.loads(row[9]), "favourite": bool(row[10]), "tags": json.loads(row[11]), "temporary": False,
                "mine": row[0] == owner, "shared_with": shared if row[0] == owner else []}

    def content(self, owner, identifier, group=None):
        """Authorize before reading audio bytes."""
        record = self.asset(owner, identifier, group)
        if not record:
            return None
        if record["temporary"]:
            return self.temporary[identifier]["content"]
        path = self.path(identifier)
        return path.read_bytes() if path.is_file() else None

    def listing(self, owner, group=None, *, kinds=KINDS, limit=100, before=None, before_id=None, job=None, tag=None, favourites=False):
        """Bounded private and shared metadata, newest first, without reading audio bytes."""
        limit = max(1, min(int(limit), 200))
        marks = ",".join("?" for _ in kinds)
        query = f"SELECT a.id FROM soundroom_assets a WHERE a.kind IN ({marks}) AND (a.owner_id=? OR EXISTS (SELECT 1 FROM soundroom_shares s WHERE s.item=a.id AND s.group_name=?))"
        values = [*kinds, owner, group or "\0"]
        if before is not None:
            if before_id:
                query += " AND (a.created<? OR (a.created=? AND a.id<?))"
                values.extend((float(before), float(before), before_id))
            else:
                query += " AND a.created<?"
                values.append(float(before))
        if job:
            query += " AND EXISTS (SELECT 1 FROM soundroom_job_assets j WHERE j.asset=a.id AND j.job=?)"
            values.append(job)
        if favourites:
            query += " AND a.favourite=1"
        if tag:
            # Filter exact tags before LIMIT on both supported database engines.
            if self.db.engine == "sqlite":
                query += " AND EXISTS (SELECT 1 FROM json_each(a.tags) WHERE value=?)"
                values.append(tag)
            else:
                query += " AND a.tags::jsonb @> ?::jsonb"
                values.append(json.dumps([tag]))
        query += " ORDER BY a.created DESC,a.id DESC LIMIT ?"
        values.append(limit)
        records = [self.asset(owner, identifier, group) for identifier, in self.db.execute(query, tuple(values)).fetchall()]
        self.expire()
        for row in self.temporary.values():
            record = row["record"]
            if row["owner"] != owner or record["kind"] not in kinds or favourites:
                continue
            if tag and tag not in record["tags"]:
                continue
            if job and record["metadata"].get("job") != job:
                continue
            if before is not None and (record["created"], record["id"]) >= (float(before), before_id or ""):
                continue
            records.append(record)
        return sorted([record for record in records if record], key=lambda record: (record["created"], record["id"]), reverse=True)[:limit]

    def update(self, owner, identifier, changes):
        """Rename, tag or favourite the caller's own retained asset."""
        record = self.asset(owner, identifier)
        if not record or record["temporary"] or not record["mine"]:
            raise ValueError("There is no such retained audio of yours.")
        if not isinstance(changes, dict) or changes.keys() - {"filename", "tags", "favourite"}:
            raise ValueError("Audio accepts filename, tags and favourite changes.")
        filename = Path(str(changes.get("filename", record["filename"])).replace("\\", "/")).name[:160] or record["filename"]
        tags = changes.get("tags", record["tags"])
        if not isinstance(tags, list) or len(tags) > 20 or not all(isinstance(one, str) and TAG.fullmatch(one) for one in tags):
            raise ValueError("Use up to 20 tags of 1–40 letters, digits, spaces or simple punctuation.")
        favourite = changes.get("favourite", record["favourite"])
        if type(favourite) is not bool:
            raise ValueError("favourite must be true or false.")
        with self.db:
            self.db.execute("UPDATE soundroom_assets SET filename=?,tags=?,favourite=? WHERE id=? AND owner_id=?",
                            (filename, json.dumps(sorted(set(tags))), int(favourite), identifier, owner))
        return self.asset(owner, identifier)

    def add_transcript(self, owner, identifier, transcript):
        """Keep a transcript with the owner's recording; the newest five are kept."""
        row = self.db.execute("SELECT metadata FROM soundroom_assets WHERE id=? AND owner_id=?", (identifier, owner)).fetchone()
        if not row:
            raise ValueError("There is no such retained audio of yours.")
        metadata = json.loads(row[0])
        metadata["transcripts"] = [transcript, *metadata.get("transcripts", [])][:5]
        with self.db:
            self.db.execute("UPDATE soundroom_assets SET metadata=? WHERE id=? AND owner_id=?", (json.dumps(metadata), identifier, owner))

    def share(self, owner, item, groups, known):
        """Replace the groups a retained asset or voice of the caller's is shared with."""
        mine = self.db.execute("SELECT 1 FROM soundroom_assets WHERE id=? AND owner_id=? UNION SELECT 1 FROM soundroom_voices WHERE id=? AND owner_id=?",
                               (item, owner, item, owner)).fetchone()
        if not mine:
            raise ValueError("There is nothing retained of yours with that ID.")
        if not isinstance(groups, list) or any(group not in known for group in groups):
            raise ValueError("Share only with existing groups.")
        with self.db:
            self.db.execute("DELETE FROM soundroom_shares WHERE item=?", (item,))
            for group in sorted(set(groups)):
                self.db.execute("INSERT INTO soundroom_shares VALUES (?,?)", (item, group))
        return sorted(set(groups))

    def delete(self, owner, identifier):
        """Delete an owned asset unless an unfinished job or a saved voice still uses it."""
        record = self.asset(owner, identifier)
        if not record or not record["mine"]:
            return False
        if record["temporary"]:
            self.temporary.pop(identifier)
            return True
        active = self.db.execute("SELECT 1 FROM soundroom_job_assets j JOIN batches b ON b.id=j.job WHERE j.asset=? AND b.status IN ('in_progress','paused','cancelling') LIMIT 1", (identifier,)).fetchone()
        if active:
            raise ValueError("This audio is used by an unfinished job; stop or finish that job first.")
        if self.db.execute("SELECT 1 FROM soundroom_voices WHERE asset=? LIMIT 1", (identifier,)).fetchone():
            raise ValueError("This recording is a saved voice's reference; delete the voice first.")
        with self.db:
            self.db.execute("DELETE FROM soundroom_job_assets WHERE asset=?", (identifier,))
            self.db.execute("DELETE FROM soundroom_shares WHERE item=?", (identifier,))
            self.db.execute("DELETE FROM soundroom_assets WHERE id=? AND owner_id=?", (identifier, owner))
        self.path(identifier).unlink(missing_ok=True)
        return True

    def save_voice(self, owner, name, asset, transcript="", description="", consent=None):
        """Save a named voice whose reference is one of the caller's retained recordings."""
        record = self.asset(owner, asset)
        if not record or record["temporary"] or not record["mine"] or record["kind"] not in ("reference", "recording", "speech"):
            raise ValueError("A voice needs one of your retained reference recordings or spoken takes.")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("Voice name must contain 1–80 characters.")
        if not isinstance(transcript, str) or len(transcript) > 4000 or not isinstance(description, str) or len(description) > 1000:
            raise ValueError("Transcript is up to 4000 characters and description up to 1000.")
        identifier = "voice_" + secrets.token_hex(12)
        with self.db:
            self.db.execute("INSERT INTO soundroom_voices VALUES (?,?,?,?,?,?,?,?)",
                            (identifier, owner, name.strip(), asset, transcript.strip(), description.strip(), consent, time.time()))
        return next(voice for voice in self.voices(owner) if voice["id"] == identifier)

    def voices(self, owner, group=None):
        """The caller's voices and those shared with the caller's group."""
        rows = self.db.execute("SELECT v.id,v.owner_id,v.name,v.asset,v.transcript,v.description,v.consent,v.created FROM soundroom_voices v "
                               "WHERE v.owner_id=? OR EXISTS (SELECT 1 FROM soundroom_shares s WHERE s.item=v.id AND s.group_name=?) ORDER BY v.name",
                               (owner, group or "\0")).fetchall()
        return [{"id": r[0], "mine": r[1] == owner, "name": r[2], "asset": r[3], "transcript": r[4], "description": r[5],
                 "consent": r[6], "created": r[7], "shared_with": self.shared_with(r[0]) if r[1] == owner else []} for r in rows]

    def voice(self, owner, identifier, group=None):
        """One visible voice, or None."""
        return next((voice for voice in self.voices(owner, group) if voice["id"] == identifier), None)

    def voice_reference(self, owner, identifier, group=None):
        """A visible voice's reference audio and transcript; a shared voice's clip is read as its owner's."""
        voice = self.voice(owner, identifier, group)
        if not voice:
            return None
        row = self.db.execute("SELECT owner_id FROM soundroom_voices WHERE id=?", (identifier,)).fetchone()
        content = self.content(row[0], voice["asset"])
        return (content, voice["transcript"]) if content else None

    def delete_voice(self, owner, identifier):
        """Delete one of the caller's voices; its recording stays until deleted separately."""
        with self.db:
            gone = self.db.execute("DELETE FROM soundroom_voices WHERE id=? AND owner_id=?", (identifier, owner)).rowcount
            if gone:
                self.db.execute("DELETE FROM soundroom_shares WHERE item=?", (identifier,))
        return bool(gone)

    def bind_job(self, identifier, owner, model_id, model_name, payload, sources=()):
        """Bind the existing durable batch to stable account and model identities."""
        for asset in sources:
            if not self.asset(owner, asset):
                raise ValueError("There is no such source audio of yours.")
        with self.db:
            self.db.execute("INSERT INTO soundroom_jobs VALUES (?,?,?,?,?,?)", (identifier, owner, model_id or "", model_name, json.dumps(payload), time.time()))
            for asset in set(sources):
                self.db.execute("INSERT INTO soundroom_job_assets VALUES (?,?,?)", (identifier, asset, "source"))

    def owner(self, identifier):
        """Resolve stable job ownership independently of mutable account names."""
        row = self.db.execute("SELECT owner_id FROM soundroom_jobs WHERE id=?", (identifier,)).fetchone()
        return row[0] if row else None

    def jobs(self, owner, limit=40):
        """Audio batch IDs and saved settings for the signed-in owner."""
        rows = self.db.execute("SELECT id,model_id,model_name,payload,created FROM soundroom_jobs WHERE owner_id=? ORDER BY created DESC LIMIT ?",
                               (owner, min(100, max(1, int(limit))))).fetchall()
        return [{"id": r[0], "model_id": r[1], "model": r[2], "settings": json.loads(r[3]), "created": r[4]} for r in rows]

    def job(self, owner, identifier):
        """Read an owned job's original request, including saved voice and source identities."""
        row = self.db.execute("SELECT payload FROM soundroom_jobs WHERE id=? AND owner_id=?", (identifier, owner)).fetchone()
        return json.loads(row[0]) if row else None

    def preferences(self, owner, settings=None):
        """Persist only validated non-secret personal preferences."""
        if settings is not None:
            if not isinstance(settings, dict) or settings.keys() - PREFERENCES.keys():
                raise ValueError("Unknown audio preference.")
            for key, value in settings.items():
                allowed = PREFERENCES[key]
                if key == 'autoplay':
                    if type(value) is not bool:
                        raise ValueError('Audio autoplay must be true or false.')
                    continue
                if value is not None and not (isinstance(value, str) and len(value) <= 160 and (allowed is None or value in allowed)):
                    raise ValueError(f"Invalid audio preference {key}.")
            with self.db:
                existing = self.preferences(owner)
                self.db.execute("INSERT INTO soundroom_preferences VALUES (?,?) ON CONFLICT(owner_id) DO UPDATE SET settings=excluded.settings", (owner, json.dumps({**existing, **settings})))
        row = self.db.execute("SELECT settings FROM soundroom_preferences WHERE owner_id=?", (owner,)).fetchone()
        return {"format": "wav", 'autoplay': False, **(json.loads(row[0]) if row else {})}
