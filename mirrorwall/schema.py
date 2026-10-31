"""Front-owned image assets, workflow presets and existing-batch ownership bindings."""
from db.migrations import Migration

MIRRORWALL = (Migration(1,"private image assets and versioned workflow presets","""
    CREATE TABLE IF NOT EXISTS mirrorwall_assets (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
        filename TEXT NOT NULL, sha256 TEXT NOT NULL, width INTEGER NOT NULL,
        height INTEGER NOT NULL, bytes INTEGER NOT NULL, created DOUBLE PRECISION NOT NULL,
        metadata TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS mirrorwall_assets_owner ON mirrorwall_assets(owner_id,created);
    CREATE TABLE IF NOT EXISTS mirrorwall_jobs (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, model_id TEXT NOT NULL,
        model_name TEXT NOT NULL, payload TEXT NOT NULL, created DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS mirrorwall_job_assets (
        job TEXT NOT NULL, asset TEXT NOT NULL, role TEXT NOT NULL,
        PRIMARY KEY(job,asset));
    CREATE TABLE IF NOT EXISTS mirrorwall_presets (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
        version INTEGER NOT NULL, model_id TEXT NOT NULL, model_name TEXT NOT NULL,
        settings TEXT NOT NULL, updated DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS mirrorwall_preferences (
        owner_id TEXT PRIMARY KEY, settings TEXT NOT NULL);
"""),Migration(2,'stable ownership for new batch uploads',"""
    CREATE TABLE IF NOT EXISTS mirrorwall_uploads (
        file_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL);
"""))
