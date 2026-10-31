"""Versioned schemas for openWeightGym's independently owned repository groups."""

from __future__ import annotations

from .migrations import Migration, add_column


SCHEDULE_GRANTS = (Migration(1, "scoped recurring work grants", """
    CREATE TABLE IF NOT EXISTS schedule_grants (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, token_hash TEXT NOT NULL,
        spec TEXT NOT NULL, enabled INTEGER NOT NULL, valid_from DOUBLE PRECISION NOT NULL);
"""),)

SCHEDULES = (Migration(1, "recurring schedules and durable occurrences", """
    CREATE TABLE IF NOT EXISTS recurring_schedules (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_id TEXT NOT NULL, owner_name TEXT NOT NULL, model_name TEXT NOT NULL,
        spec TEXT NOT NULL, grant_token TEXT NOT NULL, enabled INTEGER NOT NULL,
        next_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS schedule_occurrences (
        id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL, due_at DOUBLE PRECISION NOT NULL,
        spec TEXT NOT NULL, grant_token TEXT NOT NULL, task_token TEXT NOT NULL,
        state TEXT NOT NULL, message TEXT NOT NULL, ended_at DOUBLE PRECISION);
    CREATE UNIQUE INDEX IF NOT EXISTS schedule_one_active ON schedule_occurrences(schedule_id)
        WHERE state IN ('dispatching', 'running');
    CREATE INDEX IF NOT EXISTS schedule_history ON schedule_occurrences(due_at);
"""), Migration(2, "retain deleted schedule history", """
    ALTER TABLE recurring_schedules ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0;
"""))

ALERTS = (Migration(1, "operational alert episodes and source freshness", """
    CREATE TABLE IF NOT EXISTS operational_alerts (
        id TEXT PRIMARY KEY, condition TEXT NOT NULL, source TEXT NOT NULL,
        title TEXT NOT NULL, explanation TEXT NOT NULL, state TEXT NOT NULL,
        first_at DOUBLE PRECISION NOT NULL, last_at DOUBLE PRECISION NOT NULL,
        checked_at DOUBLE PRECISION NOT NULL, resolved_at DOUBLE PRECISION,
        recovery TEXT NOT NULL, acknowledged_by TEXT, acknowledged_at DOUBLE PRECISION,
        manual INTEGER NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS operational_alert_open
        ON operational_alerts(condition) WHERE resolved_at IS NULL;
    CREATE INDEX IF NOT EXISTS operational_alert_history ON operational_alerts(first_at);
    CREATE INDEX IF NOT EXISTS operational_alert_condition ON operational_alerts(condition);
    CREATE TABLE IF NOT EXISTS alert_sources (
        source TEXT PRIMARY KEY, checked_at DOUBLE PRECISION NOT NULL,
        available INTEGER NOT NULL);
"""),)

SETTINGS = (
    Migration(1, "settings tables", """
        CREATE TABLE IF NOT EXISTS settings_changed (
            program TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
            by TEXT NOT NULL, at DOUBLE PRECISION NOT NULL, PRIMARY KEY (program, name));
        CREATE TABLE IF NOT EXISTS settings_seen (
            program TEXT PRIMARY KEY, at DOUBLE PRECISION NOT NULL,
            told TEXT NOT NULL, refused TEXT NOT NULL);
    """),
)

GROUPS = (
    Migration(1, "groups table", """
        CREATE TABLE IF NOT EXISTS groups (name TEXT PRIMARY KEY, settings TEXT NOT NULL);
    """),
)

def _service_order(database, connection) -> None:
    """Add stable service ordering to existing configured services."""
    add_column(database, connection, "services", "position", "BIGINT")
    if database.engine == "sqlite":
        connection.execute("UPDATE services SET position=rowid WHERE position IS NULL")
    else:
        connection.execute("""WITH numbered AS (
            SELECT name, ROW_NUMBER() OVER (ORDER BY name) AS position FROM services WHERE position IS NULL)
            UPDATE services SET position=numbered.position FROM numbered WHERE services.name=numbered.name""")


OUTSIDE_SERVICES = (
    Migration(1, "outside services table", """
        CREATE TABLE IF NOT EXISTS services (name TEXT PRIMARY KEY, settings TEXT NOT NULL, position BIGINT);
    """),
    Migration(2, "stable service order", _service_order),
)


def _users_columns(database, connection) -> None:
    """Add account identity and authentication columns to legacy user tables."""
    for table, column, definition in (
        ("users", "must_change", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "totp", "TEXT"),
        ("users", "choices", "TEXT"),
        ("keys", "expires", "DOUBLE PRECISION"),
        ("users", "own", "TEXT"),
        ("keys", "via", "TEXT"),
        ("users", "identity", "TEXT"),
    ):
        add_column(database, connection, table, column, definition)


USERS = (
    Migration(1, "users and keys", """
        CREATE TABLE IF NOT EXISTS users (
            name TEXT PRIMARY KEY, grp TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            password TEXT, must_change INTEGER NOT NULL DEFAULT 0, totp TEXT,
            choices TEXT, own TEXT, identity TEXT);
        CREATE TABLE IF NOT EXISTS keys (
            hash TEXT PRIMARY KEY, user TEXT NOT NULL REFERENCES users(name) ON DELETE CASCADE
                DEFERRABLE INITIALLY IMMEDIATE,
            label TEXT NOT NULL, created TEXT NOT NULL, expires DOUBLE PRECISION, via TEXT);
    """),
    Migration(2, "legacy identity and account columns", _users_columns),
    Migration(3, "unique account identities", """
        CREATE UNIQUE INDEX IF NOT EXISTS user_identity ON users(identity);
    """),
    Migration(4, "advisory spending allowances", """
        CREATE TABLE IF NOT EXISTS spending_allowances (
            scope TEXT NOT NULL, subject TEXT NOT NULL, amount DOUBLE PRECISION NOT NULL,
            PRIMARY KEY(scope,subject));
    """),
    Migration(5, "scheduled machine-key rotation", """
        CREATE TABLE IF NOT EXISTS machine_key_rotations (
            machine TEXT PRIMARY KEY, interval_days INTEGER NOT NULL,
            owner_id TEXT NOT NULL, next_at DOUBLE PRECISION NOT NULL,
            last_error TEXT NOT NULL DEFAULT '');
    """),
    Migration(6, "client IP access registry", """
        CREATE TABLE IF NOT EXISTS client_accesses (
            ip TEXT NOT NULL, owner_id TEXT NOT NULL, owner_name TEXT NOT NULL,
            first_at DOUBLE PRECISION NOT NULL, last_at DOUBLE PRECISION NOT NULL,
            accepted BIGINT NOT NULL DEFAULT 0, failed BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY(ip, owner_id));
        CREATE INDEX IF NOT EXISTS client_accesses_owner ON client_accesses(owner_id, last_at);
        CREATE INDEX IF NOT EXISTS client_accesses_last ON client_accesses(last_at);
    """),
)


def _event_columns(database, connection) -> None:
    """Add retained audit-event metadata columns to existing stores."""
    add_column(database, connection, "events", "text", "TEXT")
    add_column(database, connection, "events", "hash", "TEXT")


def _forwarded_columns(database, connection) -> None:
    """Which machine an entry on front came from, and its number there. Compute's own entries leave both empty."""
    add_column(database, connection, "events", "machine", "TEXT")
    add_column(database, connection, "events", "source", "BIGINT")
    connection.execute("CREATE INDEX IF NOT EXISTS events_source ON events(source)")
    connection.execute("CREATE TABLE IF NOT EXISTS forwarded (through BIGINT NOT NULL)")


def _machine_telemetry(database, connection) -> None:
    """Front keeps sampled readings by physical host, alongside its event log."""
    connection.execute("CREATE TABLE IF NOT EXISTS machine_telemetry (fingerprint TEXT NOT NULL, at DOUBLE PRECISION NOT NULL, numbers TEXT NOT NULL, PRIMARY KEY(fingerprint, at))")
    connection.execute("CREATE INDEX IF NOT EXISTS machine_telemetry_at ON machine_telemetry(at)")
    connection.execute("CREATE TABLE IF NOT EXISTS machine_hosts (fingerprint TEXT PRIMARY KEY, hostname TEXT NOT NULL, names TEXT NOT NULL, last_seen DOUBLE PRECISION NOT NULL)")


def _legacy_event_source(database, connection) -> None:
    # Early SQLite history imports used source for a pathname on local events.
    # It is now the numeric forwarded ID; preserve the old value independently.
    # Neither column is included in the audit chain's at-through-text payload.
    """Assign stable source attribution to legacy audit events."""
    add_column(database, connection, "events", "legacy_source", "TEXT")
    if database.engine == "sqlite":
        connection.execute("UPDATE events SET legacy_source=source,source=NULL "
                           "WHERE machine IS NULL AND typeof(source)='text'")


LOG = (
    Migration(1, "event log", """
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at DOUBLE PRECISION NOT NULL,
            kind TEXT NOT NULL, what TEXT NOT NULL, user TEXT NOT NULL, caller TEXT NOT NULL,
            model TEXT, trace TEXT, level TEXT, detail TEXT NOT NULL, text TEXT, hash TEXT);
        CREATE INDEX IF NOT EXISTS events_kind_at ON events(kind, at);
        CREATE INDEX IF NOT EXISTS events_user ON events(user, id);
        CREATE INDEX IF NOT EXISTS events_trace ON events(trace, id);
        CREATE INDEX IF NOT EXISTS events_request_parent ON events(json_extract(detail, '$.of'), kind, what, id);
        CREATE TABLE IF NOT EXISTS traces (
            user TEXT NOT NULL, trace TEXT NOT NULL, level TEXT NOT NULL,
            at DOUBLE PRECISION NOT NULL, PRIMARY KEY(user, trace));
        CREATE TABLE IF NOT EXISTS seen (
            user TEXT NOT NULL, trace TEXT NOT NULL, hash TEXT NOT NULL,
            at DOUBLE PRECISION NOT NULL, PRIMARY KEY(user, trace, hash));
        CREATE TABLE IF NOT EXISTS texts (entry INTEGER PRIMARY KEY, asked TEXT NOT NULL, said TEXT);
    """, """
        CREATE TABLE IF NOT EXISTS events (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, at DOUBLE PRECISION NOT NULL,
            kind TEXT NOT NULL, what TEXT NOT NULL, "user" TEXT NOT NULL, caller TEXT NOT NULL,
            model TEXT, trace TEXT, level TEXT, detail JSONB NOT NULL, text JSONB, hash TEXT);
        CREATE INDEX IF NOT EXISTS events_kind_at ON events(kind, at);
        CREATE INDEX IF NOT EXISTS events_user ON events("user", id);
        CREATE INDEX IF NOT EXISTS events_trace ON events(trace, id);
        CREATE INDEX IF NOT EXISTS events_request_parent ON events((detail->>'of'), kind, what, id);
        CREATE TABLE IF NOT EXISTS traces (
            "user" TEXT NOT NULL, trace TEXT NOT NULL, level TEXT NOT NULL,
            at DOUBLE PRECISION NOT NULL, PRIMARY KEY("user", trace));
        CREATE TABLE IF NOT EXISTS seen (
            "user" TEXT NOT NULL, trace TEXT NOT NULL, hash TEXT NOT NULL,
            at DOUBLE PRECISION NOT NULL, PRIMARY KEY("user", trace, hash));
        CREATE TABLE IF NOT EXISTS texts (entry BIGINT PRIMARY KEY, asked JSONB NOT NULL, said JSONB);
    """),
    Migration(2, "captured text and hash chain", _event_columns),
    Migration(3, "preserve serialized audit evidence", "SELECT 1;", """
        DROP INDEX IF EXISTS events_request_parent;
        ALTER TABLE events ALTER COLUMN detail TYPE TEXT USING detail::text;
        ALTER TABLE events ALTER COLUMN text TYPE TEXT USING text::text;
        CREATE INDEX events_request_parent ON events((detail::jsonb->>'of'), kind, what, id);
    """),
    Migration(4, "entries forwarded to the front machine", _forwarded_columns),
    Migration(5, "front machine telemetry history", _machine_telemetry),
    Migration(6, "preserve legacy local event source paths", _legacy_event_source),
    Migration(7, "bounded optional observations and metric projections", """
        CREATE TABLE observation_spans (
            id TEXT PRIMARY KEY, request_id TEXT NOT NULL, trace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL, service TEXT NOT NULL, machine TEXT NOT NULL,
            application TEXT NOT NULL, outcome TEXT NOT NULL, error_code TEXT NOT NULL,
            at DOUBLE PRECISION NOT NULL, received DOUBLE PRECISION NOT NULL,
            finished INTEGER NOT NULL, sent INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE INDEX observation_request ON observation_spans(request_id, owner_id, at);
        CREATE INDEX observation_trace ON observation_spans(trace_id, owner_id, at);
        CREATE INDEX observation_owner ON observation_spans(owner_id, at);
        CREATE INDEX observation_filters ON observation_spans(service, machine, outcome, at);
        CREATE INDEX observation_application ON observation_spans(application, at);
        CREATE INDEX observation_error ON observation_spans(error_code, at);
        CREATE INDEX observation_forward ON observation_spans(sent, at);
        CREATE INDEX observation_time ON observation_spans(at);
        CREATE INDEX observation_received ON observation_spans(received);
        CREATE TABLE observation_sources(machine TEXT PRIMARY KEY, at DOUBLE PRECISION NOT NULL, data TEXT NOT NULL);
        CREATE TABLE observation_metrics (
            minute BIGINT NOT NULL, owner_id TEXT NOT NULL, service TEXT NOT NULL,
            machine TEXT NOT NULL, operation TEXT NOT NULL, outcome TEXT NOT NULL,
            benchmark INTEGER NOT NULL, metric TEXT NOT NULL, bucket INTEGER NOT NULL,
            count BIGINT NOT NULL, total DOUBLE PRECISION NOT NULL,
            PRIMARY KEY(minute,owner_id,service,machine,operation,outcome,benchmark,metric,bucket));
        CREATE INDEX observation_metric_time ON observation_metrics(minute, owner_id);
    """),
    Migration(8, "span policy and hourly usage summaries", lambda database, connection: _usage_hours(database, connection)),
    Migration(9, "traces of routine polls, not kept", """
        CREATE TABLE IF NOT EXISTS observation_quiet (trace_id TEXT PRIMARY KEY, at DOUBLE PRECISION NOT NULL);
    """),
    Migration(10, "caller scoped node discovery", """
        CREATE TABLE node_discovery (scope TEXT PRIMARY KEY, at DOUBLE PRECISION NOT NULL, data TEXT NOT NULL);
    """),
)


def _usage_hours(database, connection) -> None:
    """What each store keeps of its spans, and usage by hour, counted from every request ending already in the log."""
    from loadcoach.usage import ENDINGS, add_hours
    connection.execute("CREATE TABLE IF NOT EXISTS observation_policy (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("""CREATE TABLE IF NOT EXISTS usage_hours (
        hour BIGINT NOT NULL, person TEXT NOT NULL, grp TEXT NOT NULL, model TEXT NOT NULL,
        service TEXT NOT NULL, outcome TEXT NOT NULL, level TEXT NOT NULL,
        requests BIGINT NOT NULL, input_tokens BIGINT NOT NULL, output_tokens BIGINT NOT NULL,
        missing_usage BIGINT NOT NULL, recorded_cost DOUBLE PRECISION NOT NULL,
        unpriced_requests BIGINT NOT NULL, unpriced_input BIGINT NOT NULL, unpriced_output BIGINT NOT NULL,
        complete_requests BIGINT NOT NULL, complete_input BIGINT NOT NULL, complete_output BIGINT NOT NULL,
        PRIMARY KEY(hour, person, grp, model, service, outcome, level))""")
    connection.execute("CREATE INDEX IF NOT EXISTS usage_hours_person ON usage_hours(person, hour)")
    rows = connection.execute(
        f"SELECT e.at, e.user, e.model, e.what, e.level, e.detail FROM events e WHERE e.kind = 'request' "
        f"AND e.what IN ({','.join('?' for _ in ENDINGS)}) ORDER BY e.id", ENDINGS).fetchall()
    for start in range(0, len(rows), 5000):
        add_hours(connection, rows[start:start + 5000])


def _model_columns(database, connection) -> None:
    """Add current model identity and configuration columns to legacy stores."""
    add_column(database, connection, "models", "measured", "TEXT")
    add_column(database, connection, "models", "place", "INTEGER")
    add_column(database, connection, "models", "locked", "INTEGER")
    add_column(database, connection, "models", "identity", "TEXT")
    add_column(database, connection, "models", "file_revision", "INTEGER NOT NULL DEFAULT 0")


def _shared_model_presets(database, connection) -> None:
    """Convert legacy preset ownership under the existing migration transaction/lock."""
    from loadcoach.presets import migrate
    migrate(database, connection)


MODELS = (
    Migration(1, "models and fingerprints", """
        CREATE TABLE IF NOT EXISTS models (
            name TEXT PRIMARY KEY, settings TEXT NOT NULL, measured TEXT, place INTEGER,
            locked INTEGER, identity TEXT, file_revision INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS file_hashes (
            path TEXT PRIMARY KEY, size BIGINT NOT NULL, changed DOUBLE PRECISION NOT NULL, sha256 TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fingerprints (
            fingerprint TEXT PRIMARY KEY, first DOUBLE PRECISION NOT NULL, details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS measuring_run (by TEXT NOT NULL, caller TEXT NOT NULL, since TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS model_identities (
            id TEXT PRIMARY KEY, created DOUBLE PRECISION NOT NULL, removed DOUBLE PRECISION);
        CREATE TABLE IF NOT EXISTS model_names (
            model_id TEXT NOT NULL, name TEXT NOT NULL, since DOUBLE PRECISION NOT NULL,
            until DOUBLE PRECISION, PRIMARY KEY(model_id,name,since));
        CREATE INDEX IF NOT EXISTS model_names_name ON model_names(name);
    """),
    Migration(2, "model identity columns", _model_columns),
    Migration(3, "unique model identities", """
        CREATE UNIQUE INDEX IF NOT EXISTS models_identity ON models(identity);
    """),
    Migration(4, "model setup presets and undo", """
        CREATE TABLE IF NOT EXISTS model_presets (
            model_id TEXT NOT NULL, name TEXT NOT NULL, settings TEXT NOT NULL,
            updated DOUBLE PRECISION NOT NULL, PRIMARY KEY(model_id,name));
        CREATE TABLE IF NOT EXISTS model_setup_undo (
            model_id TEXT PRIMARY KEY, receipt TEXT NOT NULL, before_settings TEXT NOT NULL,
            after_settings TEXT NOT NULL, changed DOUBLE PRECISION NOT NULL);
    """),
    Migration(5, "named model load-outs", """
        CREATE TABLE IF NOT EXISTS model_loadouts (
            name TEXT PRIMARY KEY, version INTEGER NOT NULL, spec TEXT NOT NULL,
            updated DOUBLE PRECISION NOT NULL, undo TEXT);
    """),
    Migration(6, "shared model-family presets", _shared_model_presets),
)

def _role_order(database, connection) -> None:
    """Add stable ordering to existing role definitions."""
    add_column(database, connection, "roles", "position", "BIGINT")
    if database.engine == "sqlite":
        connection.execute("UPDATE roles SET position=rowid WHERE position IS NULL")
    else:
        connection.execute("""WITH numbered AS (
            SELECT name, ROW_NUMBER() OVER (ORDER BY name) AS position FROM roles WHERE position IS NULL)
            UPDATE roles SET position=numbered.position FROM numbered WHERE roles.name=numbered.name""")


ROLES = (
    Migration(1, "roles table", """
        CREATE TABLE IF NOT EXISTS roles (name TEXT PRIMARY KEY, settings TEXT NOT NULL, position BIGINT);
    """),
    Migration(2, "stable role order", _role_order),
)

MODEL_LOCATIONS = (Migration(1, "per-node model eligibility", """
    CREATE TABLE IF NOT EXISTS model_locations (
        machine TEXT NOT NULL, model_id TEXT NOT NULL, enabled INTEGER NOT NULL,
        changed DOUBLE PRECISION NOT NULL, changed_by TEXT NOT NULL,
        PRIMARY KEY(machine, model_id));
"""),)

RELIABILITY = (Migration(1, "reliability state and outcomes", """
    CREATE TABLE IF NOT EXISTS reliability_state (
        model_id TEXT NOT NULL, fingerprint TEXT NOT NULL, role TEXT NOT NULL,
        epoch INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0,
        blocked_until DOUBLE PRECISION NOT NULL DEFAULT 0,
        PRIMARY KEY(model_id, fingerprint, role));
    CREATE TABLE IF NOT EXISTS reliability_outcomes (
        request BIGINT PRIMARY KEY, at DOUBLE PRECISION NOT NULL, "user" TEXT NOT NULL,
        owner_id TEXT NOT NULL, model TEXT NOT NULL, model_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, role TEXT NOT NULL, epoch INTEGER NOT NULL,
        outcome TEXT NOT NULL, recovery INTEGER NOT NULL, feedback INTEGER);
    CREATE INDEX IF NOT EXISTS reliability_key_at
        ON reliability_outcomes(model_id, fingerprint, role, at);
"""), Migration(2, "quality ratings kept beyond the reliability window", """
    CREATE TABLE IF NOT EXISTS quality_ratings (
        request BIGINT PRIMARY KEY, at DOUBLE PRECISION NOT NULL, rater TEXT NOT NULL,
        model TEXT NOT NULL, model_id TEXT NOT NULL, fingerprint TEXT NOT NULL, role TEXT NOT NULL,
        value INTEGER NOT NULL, explanation TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS quality_model ON quality_ratings(model_id, fingerprint);
"""),)

ROLE_EVIDENCE = (Migration(1, "role evidence", """
    CREATE TABLE IF NOT EXISTS role_benchmarks (
        id TEXT PRIMARY KEY, definition TEXT NOT NULL, published DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS role_evidence (
        model_id TEXT NOT NULL, fingerprint TEXT NOT NULL, benchmark TEXT NOT NULL,
        version INTEGER NOT NULL, measured DOUBLE PRECISION NOT NULL, evidence TEXT NOT NULL,
        PRIMARY KEY(model_id, fingerprint, benchmark, version));
"""),)


def _batch_model_id(database, connection) -> None:
    """Bind retained batches to stable model identities where possible."""
    add_column(database, connection, "batch_lines", "model_id", "TEXT")


BATCHES = (
    Migration(1, "batch files and lines", """
        CREATE TABLE IF NOT EXISTS files (
            id TEXT PRIMARY KEY, user TEXT NOT NULL, filename TEXT NOT NULL,
            purpose TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, content BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS batches (
            id TEXT PRIMARY KEY, user TEXT NOT NULL, endpoint TEXT NOT NULL,
            input_file TEXT NOT NULL, output_file TEXT, error_file TEXT, status TEXT NOT NULL,
            created DOUBLE PRECISION NOT NULL, ended DOUBLE PRECISION, metadata TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS batch_lines (
            batch TEXT NOT NULL, line INTEGER NOT NULL, custom_id TEXT NOT NULL,
            body TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0, result TEXT, model_id TEXT,
            PRIMARY KEY(batch, line));
    """, """
        CREATE TABLE IF NOT EXISTS files (
            id TEXT PRIMARY KEY, "user" TEXT NOT NULL, filename TEXT NOT NULL,
            purpose TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, content BYTEA NOT NULL);
        CREATE TABLE IF NOT EXISTS batches (
            id TEXT PRIMARY KEY, "user" TEXT NOT NULL, endpoint TEXT NOT NULL,
            input_file TEXT NOT NULL, output_file TEXT, error_file TEXT, status TEXT NOT NULL,
            created DOUBLE PRECISION NOT NULL, ended DOUBLE PRECISION, metadata TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS batch_lines (
            batch TEXT NOT NULL, line INTEGER NOT NULL, custom_id TEXT NOT NULL,
            body JSONB NOT NULL, tries INTEGER NOT NULL DEFAULT 0, result JSONB, model_id TEXT,
            PRIMARY KEY(batch, line));
    """),
    Migration(2, "stable batch model identities", _batch_model_id),
)


def _task_model_id(database, connection) -> None:
    """Bind retained task records to stable model identities where possible."""
    add_column(database, connection, "external_tasks", "model_id", "TEXT")


TASKS = (
    Migration(1, "external tasks", """
        CREATE TABLE IF NOT EXISTS external_tasks (
            id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, model TEXT NOT NULL,
            fingerprint TEXT NOT NULL, subject TEXT NOT NULL, environment TEXT NOT NULL, model_id TEXT);
    """),
    Migration(2, "stable task model identities", _task_model_id),
)


def _front_task_owners(database, connection) -> None:
    """Upgrade an already deployed front-task schema using its frozen identities only."""
    import json
    add_column(database, connection, "external_tasks", "owner_id", "TEXT")
    for number, subject in connection.execute("SELECT id,subject FROM external_tasks WHERE owner_id IS NULL").fetchall():
        if owner := json.loads(subject).get("owner_id"):
            connection.execute("UPDATE external_tasks SET owner_id=? WHERE id=?", (owner, number))


FRONT_TASKS = (Migration(1, "front-owned external tasks and evidence outbox", """
    CREATE TABLE IF NOT EXISTS external_tasks (
        id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, model TEXT NOT NULL,
        fingerprint TEXT NOT NULL, subject TEXT NOT NULL, environment TEXT NOT NULL,
        model_id TEXT, machine TEXT NOT NULL, owner_id TEXT);
    CREATE TABLE IF NOT EXISTS front_task_evidence (
        task TEXT PRIMARY KEY, snapshot TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS front_task_finishes (
        task TEXT PRIMARY KEY, payload TEXT NOT NULL);
"""), Migration(2, "immutable front task ownership", _front_task_owners))


def _chat_columns(database, connection) -> None:
    """Upgrade legacy conversation metadata for current chat features."""
    add_column(database, connection, "conversations", "sampling", "TEXT NOT NULL DEFAULT '{}'")
    add_column(database, connection, "conversations", "memory", "TEXT NOT NULL DEFAULT '{}'")
    add_column(database, connection, "conversations", "compare", "TEXT NOT NULL DEFAULT '[]'")
    add_column(database, connection, "conversations", "session_mode", "TEXT NOT NULL DEFAULT 'fresh'")
    add_column(database, connection, "conversations", "sessions", "TEXT NOT NULL DEFAULT '{}'")


CHAT = (
    Migration(1, "conversations and messages", """
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, title TEXT NOT NULL,
            system TEXT NOT NULL, thinking INTEGER NOT NULL, updated DOUBLE PRECISION NOT NULL,
            sampling TEXT NOT NULL DEFAULT '{}', memory TEXT NOT NULL DEFAULT '{}',
            compare TEXT NOT NULL DEFAULT '[]', session_mode TEXT NOT NULL DEFAULT 'fresh',
            sessions TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, conversation INTEGER NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL, detail TEXT NOT NULL, at DOUBLE PRECISION NOT NULL);
        CREATE INDEX IF NOT EXISTS messages_of ON messages(conversation, id);
        CREATE TABLE IF NOT EXISTS answering (
            conversation INTEGER NOT NULL, branch INTEGER NOT NULL, user TEXT NOT NULL,
            model TEXT NOT NULL, PRIMARY KEY(conversation, branch));
    """, """
        CREATE TABLE IF NOT EXISTS conversations (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "user" TEXT NOT NULL,
            title TEXT NOT NULL, system TEXT NOT NULL, thinking INTEGER NOT NULL,
            updated DOUBLE PRECISION NOT NULL, sampling TEXT NOT NULL DEFAULT '{}',
            memory TEXT NOT NULL DEFAULT '{}', compare TEXT NOT NULL DEFAULT '[]',
            session_mode TEXT NOT NULL DEFAULT 'fresh', sessions TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS messages (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, conversation BIGINT NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL, detail TEXT NOT NULL, at DOUBLE PRECISION NOT NULL);
        CREATE INDEX IF NOT EXISTS messages_of ON messages(conversation, id);
        CREATE TABLE IF NOT EXISTS answering (
            conversation BIGINT NOT NULL, branch INTEGER NOT NULL, "user" TEXT NOT NULL,
            model TEXT NOT NULL, PRIMARY KEY(conversation, branch));
    """),
    Migration(2, "conversation sampling memory and sessions", _chat_columns),
    Migration(3, "private document collections", """
        CREATE TABLE IF NOT EXISTS collections (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL, level TEXT NOT NULL,
            model_id TEXT NOT NULL, fingerprint TEXT NOT NULL, build TEXT NOT NULL,
            model_label TEXT NOT NULL, dimensions INTEGER NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 1, created DOUBLE PRECISION NOT NULL);
        CREATE INDEX IF NOT EXISTS collections_owner ON collections(owner);
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY, collection TEXT NOT NULL, name TEXT NOT NULL, mime TEXT NOT NULL,
            original TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL,
            status TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
            created DOUBLE PRECISION NOT NULL);
        CREATE INDEX IF NOT EXISTS documents_collection ON documents(collection);
        CREATE TABLE IF NOT EXISTS document_chunks (
            id TEXT PRIMARY KEY, document TEXT NOT NULL, collection TEXT NOT NULL,
            ordinal INTEGER NOT NULL, page INTEGER NOT NULL, line INTEGER NOT NULL,
            text TEXT NOT NULL, vector TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS chunks_collection ON document_chunks(collection);
        CREATE INDEX IF NOT EXISTS chunks_document ON document_chunks(document);
    """),
    Migration(4, "private versioned prompts and fixed workflows", """
        CREATE TABLE IF NOT EXISTS prompt_versions (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
            version INTEGER NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL, created DOUBLE PRECISION NOT NULL,
            UNIQUE(owner,name,kind,version));
        CREATE INDEX IF NOT EXISTS prompts_owner ON prompt_versions(owner);
        CREATE TABLE IF NOT EXISTS workflows (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, collection TEXT NOT NULL, title TEXT NOT NULL,
            question TEXT NOT NULL, minimum DOUBLE PRECISION NOT NULL, revision INTEGER NOT NULL,
            level TEXT NOT NULL, config TEXT NOT NULL, sources TEXT NOT NULL DEFAULT '[]',
            research TEXT NOT NULL DEFAULT '{}', draft TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', epoch INTEGER NOT NULL DEFAULT 0,
            created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL);
        CREATE INDEX IF NOT EXISTS workflows_owner ON workflows(owner);
        CREATE TABLE IF NOT EXISTS workflow_attempts (
            id TEXT PRIMARY KEY, workflow TEXT NOT NULL, step TEXT NOT NULL, ordinal INTEGER NOT NULL,
            epoch INTEGER NOT NULL, state TEXT NOT NULL, receipts TEXT NOT NULL DEFAULT '[]',
            usage TEXT NOT NULL DEFAULT '{}', output TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
            started DOUBLE PRECISION NOT NULL, ended DOUBLE PRECISION);
        CREATE INDEX IF NOT EXISTS attempts_workflow ON workflow_attempts(workflow);
    """),
)


def _benchmark_server(database, connection) -> None:
    """Add serving environment identity to retained benchmark evidence."""
    add_column(database, connection, "benchmark_definitions", "server", "TEXT NOT NULL DEFAULT ''")  # empty: the benchmark's own default


def _benchmark_columns(database, connection) -> None:
    """Upgrade retained benchmark execution and sample metadata."""
    add_column(database, connection, "benchmark_definitions", "green", "TEXT NOT NULL DEFAULT 'high'")
    add_column(database, connection, "benchmark_runs", "model_id", "TEXT")


BENCHMARKS = (
    Migration(1, "benchmark records", """
        CREATE TABLE IF NOT EXISTS benchmark_definitions (
            id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, formula TEXT NOT NULL, green TEXT NOT NULL DEFAULT 'high');
        CREATE TABLE IF NOT EXISTS benchmark_state (
            id INTEGER PRIMARY KEY CHECK(id=1), paused INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS benchmark_runs (
            id TEXT PRIMARY KEY, model TEXT NOT NULL, benchmark TEXT NOT NULL, fingerprint TEXT NOT NULL,
            subject TEXT NOT NULL, environment TEXT NOT NULL, state TEXT NOT NULL,
            created DOUBLE PRECISION NOT NULL, started DOUBLE PRECISION, ended DOUBLE PRECISION,
            reason TEXT NOT NULL DEFAULT '', messages TEXT NOT NULL DEFAULT '[]', user TEXT NOT NULL,
            token TEXT NOT NULL, synced INTEGER NOT NULL DEFAULT 0, model_id TEXT);
        CREATE TABLE IF NOT EXISTS benchmark_samples (
            run TEXT NOT NULL, sample TEXT NOT NULL, at DOUBLE PRECISION NOT NULL,
            measurements TEXT NOT NULL, output TEXT NOT NULL, log TEXT NOT NULL, PRIMARY KEY(run,sample));
        CREATE TABLE IF NOT EXISTS benchmark_probe_failures (
            run TEXT NOT NULL, sample TEXT NOT NULL, attempts INTEGER NOT NULL,
            reason TEXT NOT NULL, PRIMARY KEY(run,sample));
        CREATE TABLE IF NOT EXISTS benchmark_plans (run TEXT PRIMARY KEY, plan TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS benchmark_evidence (
            run TEXT NOT NULL, sample TEXT NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY(run,sample));
    """),
    Migration(2, "benchmark display and model identity columns", _benchmark_columns),
    Migration(3, "benchmark model identity index", """
        CREATE INDEX IF NOT EXISTS benchmark_runs_model_id ON benchmark_runs(model_id);
    """),
    Migration(4, "versioned reusable benchmark datasets", """
        CREATE TABLE IF NOT EXISTS benchmark_datasets (
            suite TEXT NOT NULL, version INTEGER NOT NULL, dataset TEXT NOT NULL,
            sha256 TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, by TEXT NOT NULL, name TEXT NOT NULL,
            PRIMARY KEY(suite,version));
    """),
)

BENCHMARKS += (Migration(5, "benchmark model-server reuse setting", _benchmark_server),)


def _benchmark_text_encoding(database, connection) -> None:
    """Migrate retained benchmark output to the portable text codec."""
    from .text_codec import pack_text
    connection.execute("CREATE TABLE IF NOT EXISTS benchmark_sample_encodings ("
                       "run TEXT NOT NULL,sample TEXT NOT NULL,field TEXT NOT NULL,codec TEXT NOT NULL,"
                       "PRIMARY KEY(run,sample,field))")
    if database.engine == "sqlite":
        for run, sample, output, log in connection.execute(
                "SELECT run,sample,output,log FROM benchmark_samples "
                "WHERE instr(output,char(0))>0 OR instr(log,char(0))>0").fetchall():
            for field, value in (("output", output), ("log", log)):
                packed, codec = pack_text(value)
                if codec:
                    connection.execute(f"UPDATE benchmark_samples SET {field}=? WHERE run=? AND sample=?",
                                       (packed, run, sample))
                    connection.execute("INSERT INTO benchmark_sample_encodings VALUES(?,?,?,?)",
                                       (run, sample, field, codec))


BENCHMARKS += (Migration(6, "preserve raw benchmark text containing NUL", _benchmark_text_encoding),)


def _benchmark_location(database, connection) -> None:
    # Empty identifies evidence recorded before Turnstile froze a named compute
    # location with each run. Its environment still preserves the machine hash.
    """Add compute-location attribution to retained benchmark runs."""
    add_column(database, connection, "benchmark_runs", "location", "TEXT NOT NULL DEFAULT ''")


BENCHMARKS += (Migration(7, "benchmark compute location", _benchmark_location),)

BENCHMARKS += (Migration(8, "atomic native benchmark reservations", """
    CREATE TABLE IF NOT EXISTS benchmark_reservations (
        key TEXT PRIMARY KEY, run TEXT NOT NULL UNIQUE);
"""),)
FRONT_TASKS += (Migration(3, "cross-submitter native task reservations", """
    CREATE TABLE IF NOT EXISTS external_task_reservations (
        key TEXT PRIMARY KEY, task TEXT NOT NULL UNIQUE);
"""),)


PLUGINS = (Migration(1, "plugin use log", """
    CREATE TABLE IF NOT EXISTS uses (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at DOUBLE PRECISION NOT NULL, owner TEXT NOT NULL,
        user TEXT NOT NULL, model TEXT NOT NULL, plugin TEXT NOT NULL, instance TEXT NOT NULL,
        profile TEXT NOT NULL, session TEXT, request TEXT, outcome TEXT NOT NULL);
""", """
    CREATE TABLE IF NOT EXISTS uses (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, at DOUBLE PRECISION NOT NULL,
        owner TEXT NOT NULL, "user" TEXT NOT NULL, model TEXT NOT NULL, plugin TEXT NOT NULL,
        instance TEXT NOT NULL, profile TEXT NOT NULL, session TEXT, request TEXT, outcome TEXT NOT NULL);
"""),)

PLUGIN_RUNTIME = (Migration(1, "plugin sessions and request receipts", """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, profile TEXT NOT NULL,
        native TEXT, created DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS requests (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, profile TEXT NOT NULL,
        session TEXT NOT NULL, idem TEXT, fingerprint TEXT NOT NULL,
        status TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, ended DOUBLE PRECISION,
        response TEXT, error_code TEXT, UNIQUE(owner, profile, idem));
"""),)

SPOTTER = (Migration(1, "machine readings", """
    CREATE TABLE IF NOT EXISTS readings (at DOUBLE PRECISION PRIMARY KEY, numbers TEXT NOT NULL);
"""),)

TOOL_SCHEMAS = (Migration(1, "tool schemas", """
    CREATE TABLE IF NOT EXISTS tool_schemas (tool TEXT PRIMARY KEY, schema TEXT NOT NULL);
"""),)

# Shared by migration preflight and installer compatibility checks. Keep one schema authority.
RETIRED_COMPUTE_TABLES = (
    "keys", "users", "groups", "machine_key_rotations", "roles", "role_benchmarks", "role_evidence",
    "reliability_outcomes", "reliability_state", "benchmark_samples", "benchmark_runs",
    "benchmark_definitions", "benchmark_state", "services", "external_tasks",
    "batch_lines", "batches", "files",
)

# Retirement freezes these ledgers after their authoritative tables move to front.
RETIRED_COMPUTE_STREAMS = frozenset({
    "loadcoach.groups", "loadcoach.users", "loadcoach.roles", "loadcoach.outside",
    "loadcoach.reliability", "loadcoach.role_evidence", "loadcoach.batches", "loadcoach.tasks",
})


def _retire_compute_copies(database, connection) -> None:
    """Only the explicit DB1 command or an empty-target engine transfer applies this stream."""
    for table in RETIRED_COMPUTE_TABLES:
        connection.execute(f"DROP TABLE IF EXISTS {database.quote_identifier(table)}")


COMPUTE_RETIREMENT = (Migration(1, "retire verified front-owned compute copies", _retire_compute_copies),)


STREAMS = {
    "loadcoach.retirement": COMPUTE_RETIREMENT,
    "loadcoach.settings": SETTINGS, "loadcoach.groups": GROUPS,
    "loadcoach.outside": OUTSIDE_SERVICES, "loadcoach.users": USERS,
    "loadcoach.log": LOG, "loadcoach.models": MODELS, "loadcoach.roles": ROLES,
    "weightroom.model_locations": MODEL_LOCATIONS,
    "loadcoach.reliability": RELIABILITY, "loadcoach.role_evidence": ROLE_EVIDENCE,
    "loadcoach.batches": BATCHES, "loadcoach.tasks": TASKS, "turnstile.tasks": FRONT_TASKS, "loadcoach.schedules": SCHEDULE_GRANTS,
    "weightroom.chat": CHAT, "weightroom.benchmarks": BENCHMARKS,
    "weightroom.plugins": PLUGINS, "weightroom.alerts": ALERTS, "weightroom.schedules": SCHEDULES, "plugins.runtime": PLUGIN_RUNTIME,
    "spotter.history": SPOTTER, "toollocker.tools": TOOL_SCHEMAS,
}
