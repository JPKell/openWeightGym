"""Audio assets, kept on the front,, voices, sharing and existing-batch ownership bindings."""
from db.migrations import Migration

SOUNDROOM = (Migration(1, "private audio assets, voices, group shares and job bindings", """
    CREATE TABLE IF NOT EXISTS soundroom_assets (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
        filename TEXT NOT NULL, sha256 TEXT NOT NULL, seconds DOUBLE PRECISION NOT NULL,
        rate INTEGER NOT NULL, channels INTEGER NOT NULL, bytes INTEGER NOT NULL,
        created DOUBLE PRECISION NOT NULL, metadata TEXT NOT NULL,
        favourite INTEGER NOT NULL DEFAULT 0, tags TEXT NOT NULL DEFAULT '[]');
    CREATE INDEX IF NOT EXISTS soundroom_assets_owner ON soundroom_assets(owner_id,kind,created);
    CREATE TABLE IF NOT EXISTS soundroom_jobs (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, model_id TEXT NOT NULL,
        model_name TEXT NOT NULL, payload TEXT NOT NULL, created DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS soundroom_job_assets (
        job TEXT NOT NULL, asset TEXT NOT NULL, role TEXT NOT NULL,
        PRIMARY KEY(job,asset));
    CREATE TABLE IF NOT EXISTS soundroom_voices (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
        asset TEXT NOT NULL, transcript TEXT NOT NULL, description TEXT NOT NULL,
        consent DOUBLE PRECISION, created DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS soundroom_shares (
        item TEXT NOT NULL, group_name TEXT NOT NULL,
        PRIMARY KEY(item,group_name));
    CREATE TABLE IF NOT EXISTS soundroom_preferences (
        owner_id TEXT PRIMARY KEY, settings TEXT NOT NULL);
"""),)
