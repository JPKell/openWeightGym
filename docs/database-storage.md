# openWeightGym database storage and operations

DB1 moved task/settings ownership and missing history to front on SQLite, then retired
17 stale compute tables. Models and live operational state remain on compute.
[Ownership and recovery instructions](operations.md#db1-ownership-and-retirement-milestone-124).
Schema delivery status is recorded in the [agent ledger](agent-status.md#milestones).

Mirror Wall uses the existing front `accounts.db`: `mirrorwall.front` owns private asset metadata,
stable account/job/upload bindings, workflow versions and preview preferences. Binary originals
and thumbnails live under front `data/mirrorwall-assets/` and join the existing recovery inventory;
temporary/no-log assets never enter SQL, files or recovery sets. Workers retain model bundles
and measured workflow resource envelopes in their existing model registry. Source igen data stays intact.

`mirrorwall.front` migration 3 retains workflow revisions in `mirrorwall_preset_versions`, seeded
with each existing workflow's latest saved version; older overwritten settings cannot be recovered.
The current workflow and its revision commit together, stale saves refuse to overwrite newer
versions, and history access follows the parent workflow's private/shared permissions. Existing
full-store backup/export/transfer includes the new table. A code rollback leaves it intact, but
older code will not append history, so restore a verified pre-upgrade recovery set if reverting
the store as well. No model files or live settings are moved by this migration.

SoundRoom uses the same front `accounts.db`: `soundroom.front` owns private audio asset metadata
(takes, sounds, speech, references, recordings and their transcripts), saved voices with their
consent time, group shares, job bindings and personal preferences. Audio files live under front
`data/soundroom-assets/` and join the recovery inventory; temporary/no-log audio never enters SQL,
files or recovery sets. Compute keeps installed audio packages, each with a `.suite-package-*.json`
record of its source revision and hashes, under the `audio` model place.

Local **2026-10-15**, shared model presets / milestone 110: `loadcoach.models` migration 6 adds
`shared_model_presets` keyed by model-family/file/entry scope and preset name. It atomically
converts existing `model_presets`, retaining duplicate names with source-entry suffixes and
leaving that legacy table empty. The migration reads GGUF headers only; models, measurements,
identities and `model_setup_undo` are unchanged. Family/file presets survive entry deletion;
missing-file entry presets keep the prior identity boundary. Existing backup/export/verified
transfer discovers the new table on SQLite/PostgreSQL. Code-only
downgrade will not restore moved presets; rollback requires the verified pre-upgrade core-store
recovery set with services stopped. No store, service or production data move is added.

Local **2026-10-15**, named load-outs / milestone 97: `loadcoach.models` migration 5 adds
`model_loadouts` (name, revision, stable-ID/card/context selection, timestamp and batch undo
pointer) to the existing compute-owned core store. All member settings and their ordinary
`model_setup_undo` receipts commit or roll back together in one transaction on SQLite/PostgreSQL.
Guarded undo refuses a later member edit. Definitions and undo participate in existing full-store
backup/export/verified transfer; native runtime leases stay volatile and do not restart models.
No new store or production data move is introduced.

Local **2026-10-15**, C04 / milestone 94: `weightroom.benchmarks` migration 4 adds immutable
`benchmark_datasets` to the existing front-owned benchmark store, keyed by suite/version with
canonical data, SHA-256, display name, timestamp and author. Runs freeze their chosen version
before remote task creation; old versions remain available for history and restart. Existing
full-store backup/restore, SQL exports and verified transfer discover this table automatically.
No store move or additional database is introduced.

Local **2026-10-15**, C05 / milestone 93: `loadcoach.models` migration 4 adds model-specific
`model_presets` and one `model_setup_undo` receipt per model to the existing compute-owned core
store. Both use stable model IDs and portable SQL on SQLite/PostgreSQL. A reviewed change uses
compare-and-swap on the stored settings, sort order, identity and file revision, writing its
undo receipt in the same transaction. Undo restores settings, not file bytes or measured GPU
state; changed file selections advance the existing file revision even when undone. Renames
keep these rows; model removal deletes undo. Milestone 110 supersedes per-entry preset ownership
with the shared scope and migration described above. Existing full-store backup/restore/export and
verified transfer discover the tables automatically. No data move or new store is introduced.

Storage qualification reviewed on 2026-10-16; live store bindings remain SQLite.

## Current answer

openWeightGym services can use legacy SQLite paths, named SQLite stores or named PostgreSQL stores today.
P1 through P5 are implemented: the shared `db/` package owns connection construction, transactions,
driver operations, named service bindings, numbered schema migrations and verified engine-specific
backup/restore operations. Different machines, services and service-owned repositories can select
independent stores and engines.

Active repositories now use portable parameter handling and conflict operations, with explicit
engine expressions where JSON scalar semantics differ. PostgreSQL schemas use generated identity
columns and JSONB for batch payloads and retained request text while returning the serialized JSON
contract existing repository code expects. Existing SQLite files are upgraded in place through
the same recorded migration streams and remain the default.

Audit event `detail` and `text` remain TEXT: their original JSON serialization is part of the
hash chain. The PostgreSQL event-parent index casts that text to JSONB. Log migration 3 converts
the earlier PostgreSQL JSONB columns to TEXT; it cannot recover serialization already lost by
an older installation. Migration preflight refuses an invalid event chain and never recalculates
historical hashes. PostgreSQL event writers take an exclusive table lock around the last-hash
read and append so concurrent services extend one chain while readers remain available.

The DB page inventories and exports SQLite and PostgreSQL stores, and recovery sets verify both
engines. P4 adds an operator-profile migration wizard with offline copy, acceptance and rollback.
P4 was implemented locally on **2026-10-13** and qualified against isolated PostgreSQL 18.6 copies on **2026-10-16**. It has not switched production stores. PostgreSQL
recovery requires compatible native `pg_dump` and `pg_restore` programs on the machine that owns
the configured store; the page reports their availability.
P5 health/backup-age code and panels match the installed front/compute files; isolated native PostgreSQL 18 signals passed on **2026-10-16**. The lossless migration, inventory, health and installer fixes below were deployed and live-verified on both primary hosts on **2026-10-16**. SQLite remains the installation default.

[`scripts/install-suite.sh`](../scripts/install-suite.sh) now selects services and storage for new
machines, audits existing schemas and native PostgreSQL tools, and can install PostgreSQL and
pgvector. It continues to refuse direct installer engine switches on populated stores; use
the separate P4 maintenance workflow. See the [installation guide](installation.md).

The active code and the repository README determine current storage support and deployment scope.

## Live database inventory

The 2026-10-16 inventory resolves **13 SQLite stores**: nine primary front/compute stores and
four independent CPU-worker stores, with their bindings, writers and file-backed assets. System → DB also discovers other top-level `.db` files.
Protected receipts are under `data/qualification/g14-postgresql18/evidence/` on compute;
front receipts are under `~/.local/share/suite-pg18-qualification/evidence/`.

| Machine | Store | Writers / ownership |
| --- | --- | --- |
| Front | `accounts.db` | WeightRoom, Turnstile and ToolLocker: accounts, client access, events, tasks, front-program/ToolLocker settings, tool schemas and schedule grants |
| Front | `roles.db` | WeightRoom and Turnstile: roles, benchmark evidence, reliability and per-node model placement enablement |
| Front | `chat.db` | WeightRoom: chats, collections, prompt versions and workflows |
| Front | `benchmarks.db` | WeightRoom benchmark worker: plans, samples, frozen datasets and evidence |
| Front | `plugins.db` | WeightRoom and Turnstile: plugin-use records |
| Front | `alerts.db` | WeightRoom alert collector and acknowledgement controls |
| Front | `schedules.db` | WeightRoom recurring scheduler and occurrence controls |
| Compute | `loadcoach.db` | LoadCoach and Spotter: models, live events/settings and identities; frozen former ToolLocker rows retained for recovery |
| Compute | `spotter.db` | Spotter telemetry |
| Front CPU | `data/front-cpu/loadcoach-front-cpu.db` | Independent CPU LoadCoach registry/live log; shared Spotter identity binding |
| Front CPU | `data/front-cpu/spotter-front-cpu.db` | CPU host Spotter history |
| Remote CPU | `/home/jpk/suite-cpu/data/loadcoach-remote-cpu.db` | Independent CPU LoadCoach registry/live log; shared Spotter identity binding |
| Remote CPU | `/home/jpk/suite-cpu/data/spotter-remote-cpu.db` | CPU host Spotter history |

Current active consumers are WeightRoom, Turnstile, ToolLocker and Voice gateway on front; LoadCoach,
Spotter and Voice on compute; and independent LoadCoach/Spotter CPU workers on front
and remote. Voice uses the owning APIs and, when configured, writes optional observations to
its owner's log store; the gateway uses front accounts and compute Voice uses compute core.
LOG migration 7 adds `observation_spans`, `observation_metrics` and `observation_sources`;
migration 8 adds `observation_policy` (front's span limits) and `usage_hours` (hourly usage
summaries, backfilled from existing request endings); migration 9 adds `observation_quiet`
(trace IDs of swept routine polls, kept a day);
front owns retained reporting while node and adapter stores provide bounded optional spools.
[Observation durability and retention](loadcoach.md#observability-and-traceability-milestone-151).
Maintenance must also
stop operator CLI/direct database writers. DB1 ownership remains authoritative; retired compute
ledgers can stay at their recorded version and are replayed only to that version before retirement.

Both halves of recovery set `20261017T003223Z-24b1f8` were independently extracted and verified,
then the nine primary stores completed SQLite → PostgreSQL 18.6 → SQLite transfers and native
PostgreSQL custom-dump restores. Each transfer checked table counts/content hashes, schema ledgers,
event chains, identity sequences and referenced model artifacts; 35 model references remained in place.
The four independent CPU-worker stores passed the same isolated round-trip and native restore
checks: receipts are in `data/qualification/g14-postgresql18/evidence/front-cpu/result.json` and
`data/qualification/g14-postgresql18/evidence/remote-cpu-result.json`; the latter was copied from
the remote host's protected qualification directory.
Source snapshots stayed unchanged; live bindings remain SQLite. [Protected DB1 receipts and recovery](operations.md#db1-ownership-and-retirement-milestone-124).

## Front ToolLocker and remaining telemetry (milestone 128)

Authorized, deployed and live-verified **2026-10-16**. Front's existing accounts store now
owns ToolLocker settings, recorded schemas, trace levels and chained tool-call events.
The public MCP address stays `10.77.10.42:8443`; Turnstile signs requests to front's
loopback ToolLocker at `127.0.0.1:9443`. Compute file/code tools run through fixed SSH
stdio commands under their existing server names and roots; file bytes stay on compute.
The existing front HTTP tool host and its private key remain in use. No hosted tool was added.

Exact imports preserved 11 recorded tool schemas and the program report; there were no
site overrides to move. All nine currently offered tool schemas matched before/after.
Compute's former records remain frozen recovery originals: the disabled gateway refuses
startup with `front_toollocker = true`, and compute API/CLI refuse their settings/reports.
Front backup inventory identifies ToolLocker as an accounts-store writer. Production
bindings remain SQLite; a PostgreSQL engine switch is separately authorized work.

Verified physical-host snapshots added **611 front** and **632 VPS** missing telemetry
readings. Every source row compared exactly after import, and repeat imports added zero.
Local histories remain live; normal front history retains its existing 28-day policy.

Protected snapshots, SHA-256 receipts, exact imports, before/after schemas and live checks
are under `~/.local/share/suite-db-audits/2026-10-16-db2/` on compute and front. The remote
source snapshot is retained at that same path on the VPS. See [operating and rollback steps](operations.md#front-toollocker-relocation-milestone-128).

Live qualification covered signed tools, permission filtering, revocation, audit-chain and
trace continuity, front Tools/Settings/Telemetry while primary LoadCoach was stopped, and
prompt retryable 503/recovery when the gateway itself stopped. Restart and paired recovery
receipts are stored with those protected checks. Pre-cutover pair `20261017T042002Z-2bb9f5`
and final pair `20261017T042722Z-81134d` passed isolated restores. The final manifest names
ToolLocker as a front accounts-store writer/service; both outside archive copies match SHA-256.
Local checks passed: 124 focused checks, then 81 console/recovery checks including the
normalized-settings ownership regression; suite counts overlap. Commands are in `local-checks.json`.

## Where SQLite remains coupled into the code

Repository and maintenance modules no longer call `sqlite3.connect`; connection creation is
centralized in `db/drivers/sqlite.py`. Live repository schemas and queries support both engines.
The remaining engine-specific work is operational:

- LoadCoach constructs several repositories over the same `loadcoach.db`: events and captured
  text, users and keys, groups, models and fingerprints, roles/evidence, reliability, settings,
  outside services, batch files/lines and external tasks.
- ToolLocker and Spotter independently open that same file for shared identity, authorization,
  settings and event logging. Spotter also owns `spotter.db`.
- `components.versions` keeps each host’s shared service Git tag/commit and engine-reported versions in
  `loadcoach.core` (front-only installs use `weightroom.accounts`). Spotter checks them every 60 minutes;
  installation forces a refresh. Failed probes display Unknown; Components reads saved versions without loading models.
- WeightRoom owns `chat.db` and `plugins.db`; its embedded benchmark worker owns
  `benchmarks.db`.
- Each repository invokes its own numbered migration stream under an SQLite immediate lock or a
  PostgreSQL advisory lock. The migration ledger rejects renamed history and failed migrations
  roll back without advancing their recorded version.
- SQLite dump/inventory, backup verification and the legacy chat-export utility intentionally use
  `PRAGMA`, `sqlite_schema` and SQLite's online-backup API. PostgreSQL maintenance uses its native
  catalogue, `pg_dump` and `pg_restore` through the P3 driver.
- Repositories remain synchronous DB-API users. The shared connection proxy owns parameter-style
  adaptation and preserves transaction/context behavior across SQLite and psycopg; literal
  percentages are escaped for bound PostgreSQL queries, and failed commits roll back.
- Recovery sets snapshot SQLite files and verify them with `PRAGMA integrity_check`; PostgreSQL
  stores use custom logical dumps and isolated restore verification.
- Routine Backups inventory reads reachability, file size and schema versions without full
  integrity or row-count scans. Creating and restoring a recovery set retains those full checks.

The PostgreSQL 18.6 repository/native recovery contracts passed using front's loopback container
and matching native clients. Compute used privately unpacked 18.6 clients under the qualification
workspace through an SSH tunnel; those binaries are not installed service dependencies. SQLite
backup/export continues to work without PostgreSQL clients. The installer audits both `pg_dump`
and `pg_restore` majors against the server when PostgreSQL recovery tools are required.

## Data and availability requirements

A storage migration must preserve more than table row counts:

- account identities, password verifiers, authenticator secrets, API-key hashes and permissions;
- the event hash chain and retained request text;
- stable model identities, names, fingerprints and file revisions;
- batch input/output blobs and external-task credentials;
- benchmark plans, raw answers, measurements and evidence;
- chat branches, messages, attachments and summaries;
- settings provenance and program acknowledgements;
- file-backed model weights, plugin settings/keys, voice assets and other artifacts that are
  referenced by rows but do not live in a database.

Database credentials must remain in owner-only key files or environment-managed secrets. They
must not appear in browser URLs, normal TOML settings, event logs, backup manifests or migration
reports. A migration must be an explicit maintenance operation: drain writers, take a verified
backup, copy once, validate, switch every service together, and retain a rollback point. openWeightGym
must not silently dual-write two engines.

## Deployment and store-binding requirements

The storage layer must support more than one configured database. `Database` is a shared class
and contract, not a process-wide singleton: a process can create several instances, one for each
named store it uses. A small `DatabaseRegistry` resolves logical bindings to those instances.

Configuration defines named stores separately from service bindings. Each machine may provide a
different set of stores, and each service or service-owned repository may select the store that
owns its data. For example, WeightRoom chat can use local SQLite while its benchmark worker uses
PostgreSQL, and LoadCoach on another machine can use a separate PostgreSQL database. A service is
not limited to one store and two services are not required to use the same engine.

The intended configuration model is equivalent to:

```toml
[databases.stores.local_chat]
engine = "sqlite"
path = "data/chat.db"

[databases.stores.shared_benchmarks]
engine = "postgresql"
dsn_file = "secrets/benchmarks.dsn"

[databases.bindings]
"weightroom.chat" = "local_chat"
"weightroom.benchmarks" = "shared_benchmarks"
```

Logical data ownership remains explicit. Repositories that participate in one transaction must
bind to the same store. Services sharing authoritative identity, permissions, event-chain data or
other consistency-sensitive records must either use that same store or access the owner through
an API; openWeightGym must not copy or silently synchronize those records between independent databases.

Progression P5 / milestone 70 adds the front-owned `weightroom.alerts` binding, defaulting to
`data/alerts.db`. Its `weightroom.alerts` migration stream owns operational episodes and source
freshness. It supports SQLite or PostgreSQL independently and is included in named-store health,
exports, verified recovery, offline transfer and installer compatibility checks. Its collector
pauses at maintenance, counts in-flight collection in the drain and discards reads that cross
the barrier. It contains sanitized observations and acknowledgement, not authoritative identity
or raw journal/prompt text. Progression P5 was accepted as complete on 2026-10-14 after live
source/background collection checks and a deployed-code temporary-store lifecycle/persistence
fixture. This qualification did not induce a production incident or live service restart.

Progression P6 / milestone 71 adds front's `weightroom.schedules` binding, defaulting to
`data/schedules.db`, with the portable `weightroom.schedules` stream for definitions and
occurrences. Compute's `loadcoach.schedules` stream stores hashed job grants in the core store.
Both engines support them; backup, export, offline transfer and installer audit include them.
Front's database contains job-specific schedule/task tokens, protected like benchmark tokens;
compute stores grant hashes. These are limited durable-work credentials, not general API keys.
Maintenance tracks active scheduler writers and defers further claims and dispatch commits.
There are no cross-store joins or atomic cross-store transactions. References across stores use
stable IDs and are validated by the owning service.

SQLite stores must be local to the process host. A remote or multi-writer store uses a server
engine such as PostgreSQL. Credentials remain in owner-only files and service configuration
contains only store names and non-secret display metadata.

## PostgreSQL implementation plan

### P0 - inventory, dumps and migration workspace

Add System -> DB for accounts with `manage-settings`. It inventories front and compute database
files, tables, record counts, table/index bytes and total bytes on disk. It exposes restorable
per-table SQL dumps and full-database dumps. Native `sqlite3` is used when available; Python's
SQLite API is the fallback. The page reports detected SQLite/PostgreSQL tools and the actual
engine capability instead of presenting PostgreSQL as usable prematurely.

This phase is the implementation that accompanies this document.

### P1 - storage contract and configuration

Introduce one small DB-API storage package used by every repository. It contains the shared
`Database` facade, a driver contract, SQLite and PostgreSQL drivers, a named-store registry and
engine-neutral configuration. It must provide connection and transaction scopes, placeholder
adaptation, identifier quoting, JSON operations, upsert helpers, schema inspection and advisory
migration locks for both engines. Add `psycopg` as a PostgreSQL optional dependency and test
dependency.

Replace file-only `database` settings with named engine-neutral stores and explicit bindings for
each service-owned repository. SQLite stores keep a path. PostgreSQL stores use a DSN read from an
owner-only file, with a non-secret display name in settings. Validate bindings at startup and
reject missing stores, unsupported engines, remote SQLite paths and inconsistent bindings for
repositories that share a transaction or authoritative dataset.

Implemented on 2026-10-13. The package is `db/`, and all active connection sites now go through
its facade. Current binding names are `loadcoach.core`, `weightroom.chat`,
`weightroom.benchmarks`, `weightroom.plugins`, `spotter.identity`, `spotter.history` and
`toollocker.identity`. Existing path settings remain compatible. Store paths are resolved from
the service settings directory, PostgreSQL DSNs are read at connection time from owner-only files,
and registry descriptions never contain DSN contents. Contract tests cover independent stores,
commit/rollback, migration locking, placeholders, quoting, JSON, upserts and schema inspection.

### P2 - versioned schemas and repository conversion

Move constructor DDL into numbered, transactional migrations. Define equivalent SQLite and
PostgreSQL schemas, including JSONB/index expressions, generated identity columns, foreign keys
and conflict behavior. Convert repositories one bounded group at a time:

1. settings, groups and outside services;
2. users, keys, traces and replay protection;
3. events, captured text and usage queries;
4. models, identities, fingerprints, roles and reliability;
5. batch files/lines and external tasks;
6. chat, benchmarks, plugins and Spotter telemetry.

Run each repository contract suite against temporary SQLite and PostgreSQL databases. Also run
mixed-store deployment tests in which chat, benchmarks, telemetry and LoadCoach use different
stores or engines. PostgreSQL support is not complete until the full normal test suite passes
against both engines and the binding tests prove that no repository opens an unconfigured path or
bypasses the registry.

Implemented on 2026-10-13. `suite_schema_migrations` records independent streams for settings,
groups, users, services, events, models, roles, reliability, role evidence, batches, external
tasks, conversations, benchmarks, plugin records/runtime state, Spotter readings and ToolLocker
tool schemas. Constructors invoke these streams instead of embedding DDL. Existing additive
SQLite upgrades are represented as ordered migrations, and PostgreSQL uses generated identities,
deferrable user-key references, JSONB payloads and expression indexes where the engines differ.

The normal SQLite suite and PostgreSQL repository/application contracts cover CRUD, event-chain
IDs, usage JSON aggregation, account renaming, batch counts, chat identities, benchmark state,
plugin activity, telemetry/tool stores and mixed SQLite/PostgreSQL bindings. PostgreSQL tests are
enabled with `SUITE_TEST_POSTGRES_DSN_FILE`; without it they skip rather than requiring a server
for SQLite-only installations.

### P3 - backup, dump and restore drivers

Keep SQLite online snapshots and integrity checks. Add PostgreSQL logical backups with `pg_dump`
in custom format, inspect them with `pg_restore --list`, and perform a restore into an isolated
temporary database before calling a backup verified. Per-table PostgreSQL exports use `pg_dump`
with an exact, safely quoted table selection. Record tool versions, source server version,
schema version, logical store name, owning services and checksums in manifests without recording
credentials. Recovery sets enumerate every configured store on every machine and report a missing
or unreachable required store instead of silently omitting it.

Implemented on 2026-10-13. Recovery manifests are schema 2 and describe each logical store with
its engine, non-secret display name, repository bindings, owning services, source server version,
migration-stream versions, native tool versions, archive checksum and restore evidence. SQLite
stores continue to use the online backup API and integrity checks. PostgreSQL stores use
`pg_dump --format=custom`; openWeightGym inspects the archive with `pg_restore --list`, creates an isolated
temporary database, restores with owner and ACL changes disabled, compares every application-table
count and migration-stream version to the source, and removes the temporary database before the
recovery half is marked verified.

The DB page inventories configured PostgreSQL stores and exports either the complete database or
one exact, catalog-validated table as plain SQL. PostgreSQL credentials are read from the existing
owner-only DSN file. The password is removed from the command-line connection string, supplied only
to the child-process environment and never written to manifests or errors. Each machine enumerates
its configured stores before adding discovered SQLite files; a required missing or unreachable
store fails recovery-set creation instead of producing a misleading partial inventory. The host
must provide compatible `pg_dump` and `pg_restore` programs before a PostgreSQL recovery can run.

### P4 - offline engine migration

Implemented locally on **2026-10-13**, after P1-P3. A migration selects one named
store and updates only its declared bindings; stores may therefore move engines or machines
independently when their consistency boundaries permit it. It performs:

On **2026-10-16**, the owner authorized preparing a local target. Front now has an empty
PostgreSQL 18.6 container named `suite-p4-postgres`, bound only to `127.0.0.1:55432`, with a
persistent `suite-p4-postgres-data` Docker volume. The non-superuser `suite_app` owns the
`suite_front` database; the owner-only connection file is
`/home/jpk/weightroom/keys/postgres-p4.dsn`. Native `pg_dump` and `pg_restore` 18.6 are installed
on front. A scratch database containing identity, text and JSONB columns passed a native
dump and isolated restore, and the target database remains empty. An initial 16 server failed
that restore because the available 18 client emitted `transaction_timeout`; it was replaced
before any openWeightGym data was copied. No production binding has changed. PostgreSQL 18 repository
contracts and isolated 13-store qualification have since passed; a fresh reviewed maintenance
preview still precedes any separately selected live cutover. This loopback target is for front-owned
stores; compute-owned stores require their own target or a deliberately networked database.

The isolated G14 qualification on **2026-10-16** completed the current inventory and both
verified recovery halves, all 13 store round trips/native restores, private profile checks,
six-category repository fixtures, accepted cutover, failed-acceptance rollback and interrupted-job
recovery. These fixtures change disposable configurations only and make no live engine switch.
Native PostgreSQL health probes also passed verified TLS, connection pressure, a read-only login
and disabled-autovacuum warnings in a separate disposable container.

Qualification found and fixed three lossless compatibility issues:

- Log migration 6 retains a historical local event's pathname in `events.legacy_source` and clears
  its invalid numeric `source`; event IDs, payload serialization and audit hashes stay exact.
- Benchmark migration 6 adds `benchmark_sample_encodings`. Only raw output/log fields containing
  NUL use explicit base64 UTF-8 storage; reads, grading and retries retain the original text. One
  retained answer required encoding. Full-store exports, recovery and transfer include the metadata.
- Retired compute streams retain their recorded versions during transfer, preventing newer
  front-only tables from reappearing. Inventory accepts explicit checkout/config service commands
  without `WorkingDirectory` and ignores the retired users-mirror setting.

Matching `db/`, `benchmarks/store.py` and installer support were deployed on **2026-10-16**, retaining
all nine primary SQLite bindings. Both halves of the fresh pre-upgrade recovery set
`20261017T015926Z-a540a3` restored independently; the restarted services passed schema, integrity,
audit payload/hash, decoded benchmark-text, health/freshness and authenticated API/page checks.
Direct-compute and front unauthenticated status requests returned 401. The temporary verification
key was revoked, both maintenance markers were released, and the original PostgreSQL target remains
empty. Receipts are in `data/qualification/g14-postgresql18/deployment/`. A code-only downgrade
cannot decode encoded benchmark rows: restore the retained pre-upgrade snapshot with writers stopped;
local old-code rollback bytes are in `deployment/pre-upgrade-code.tar.gz`, and front
old-code copies are under `~/.local/share/suite-pg18-qualification/pre-upgrade-code/`. No engine switch occurred.

1. preflight source/target connectivity, versions, free space, tools and schema compatibility;
2. a paired verified openWeightGym recovery set and an artifact inventory;
3. writer drain and maintenance lock across WeightRoom, LoadCoach, ToolLocker, Spotter and turnstile;
4. target schema creation and deterministic table copy;
5. sequence repair, row counts, selected content hashes, event-chain validation and artifact
   reference checks;
6. a generated binding and configuration preview with secrets referenced by file;
7. coordinated stop of every service bound to that store, atomic binding switch and restart;
8. sign-in, model inventory, request, chat, benchmark and tool smoke checks;
9. rollback to the original engine if any acceptance check fails.

Migration back to SQLite uses the same logical contract and checks. The source remains untouched
until the new installation passes acceptance; deletion is a separate later action.

The implementation is `db/transfer.py`, `db/migration.py` and `db/maintenance.py`, with the wizard
under System → DB. Profiles select the source by an existing repository binding, resolve every
other binding to the same physical store, and generate changes only for those bindings. This
includes ToolLocker and Spotter's implicit use of LoadCoach's identity store. Turnstile now resolves
`weightroom.plugins` from the same front settings as WeightRoom, so migrating plugin records
does not leave turnstile writing a legacy SQLite file. All active openWeightGym
user services on the declared installations are stopped for the maintenance window, including
turnstile and voice consumers; unrelated store selections are preserved. There is no dual writing.

Preflight requires current, recognized schema streams and tables, matching columns and primary
keys, recognized indexes, no custom views/triggers, source integrity, an intact retained event
chain, accessible artifacts, an empty target, compatible PostgreSQL tools and free-space reserve.
The PostgreSQL server filesystem receives a separate operator-configured free-space probe.
Copy uses explicit columns and deterministic primary-key ordering, retains original IDs and
BLOB/BYTEA content, and repairs sequence high-water values even when the highest row was deleted.
Every table gets a row-count and SHA-256 content comparison independent of engine collation.
Only columns intentionally stored as JSONB use normalized JSON comparisons; duplicate keys are
rejected. Validation also reopens the committed target independently. Reports contain counts,
hashes and safe metadata, never row contents, DSNs, passwords, session keys or maintenance tokens.

The persistent maintenance marker rejects new HTTP/WebSocket clients and counts existing
requests through the end of streaming responses. Background batches, benchmark supervision and
new probes, housekeeping, hashing, settings following and Spotter telemetry/idle sleep pause.
The coordinator drains admitted work and waits for user-service shutdown before the final paired
backup and copy. Restarted services remain behind the barrier while acceptance runs; ordinary
traffic and deferred work resume only after acceptance. Existing CLI scripts or external programs
that write directly to a store must also be stopped by the operator before maintenance.

Each configuration file is staged and replaced atomically while all services are stopped.
A partial multi-file or multi-host switch restores every staged original before traffic can
resume. Configuration snapshots, source stores and failed targets remain available for recovery.
Rollback verifies all six functional acceptance categories against the original installation.
If rollback fails, services are stopped and maintenance stays closed for operator recovery.

#### Configure a migration

1. Install matching openWeightGym code on every participating host and restart the owning services so
   their schemas and maintenance barriers are current. No production P4 deployment is implied
   by the local implementation status above. The existing front deployer copies `db/`; use its
   full deployment mode when turnstile, ToolLocker or Spotter code also needs updating.
2. Provision a dedicated empty PostgreSQL database and an owner-only DSN file, or create the
   parent directory for a new SQLite target file. PostgreSQL credentials and client tools must
   be available on the source owner; every host receiving a PostgreSQL binding must independently
   have the DSN file at the previewed absolute path. The source owner can connect to a PostgreSQL
   server on another machine. SQLite targets remain local to their owning service host; a shared
   remote store cannot be rebound to separate local SQLite files.
3. On the coordinator (normally front), copy
   [`settings/examples/database-migration.json`](../settings/examples/database-migration.json)
   to `settings/migrations/core-to-postgresql.json` with mode `600`. Set `owner`, `binding`, the
   complete `hosts` map, target, target server filesystem probe and acceptance file. Use one host
   entry for a combined installation. A remote host uses passwordless account SSH in `ssh` and
   an absolute openWeightGym checkout in `root`; the helper runs that installation's `.venv/bin/python`.
   A profile must include every installation sharing or writing the selected store. The browser
   selects a bounded profile name; it never supplies hosts, commands, DSNs or filesystem paths.
4. Copy
   [`settings/examples/database-migration-smoke.json`](../settings/examples/database-migration-smoke.json)
   to the private `smoke_file` location with mode `600`, replacing every `REPLACE_WITH_...` fixture.
   Choose an existing acceptance account, visible local model, owned chat, completed benchmark
   evidence and read-only tool. The model request generates a short answer; chat and benchmark
   checks read existing saved evidence. The checks run both on the target and on rollback.
   They require content assertions in all six categories, not just successful HTTP statuses.
   Sign-in headers and password bodies live in separate owner-only JSON files named by
   `headers_file` and `body_file`. A registered sign-in application's header file has an
   `authorization` entry; the body file has `name` and `password` (and `code` if required).
   A fixed authenticator code can expire during a long migration, so prepare acceptance access
   appropriate to the maintenance window. `${session}` is extracted from the sign-in result
   and retained in worker memory only. JSON assertion paths use dot-separated keys/array indexes;
   `contains` checks saved text or HTML. Credentials and response bodies never enter reports.
5. Open System → DB → the profile → **Check and preview**. Review the target, artifacts,
   affected bindings, generated TOML and services to stop. **Start reviewed migration** repeats
   preflight and refuses changed settings, services, profile or acceptance files. Private fixture
   fingerprints use a local owner-only HMAC key, preventing password-file digests from being
   exposed in the preview. A queued lease and one maintenance lock per installation reject
   overlapping jobs and repeated clicks.

Additional required file references outside discovered keys, plugin settings and voice assets
can be listed in the profile's `artifacts` array. Model/projector/LoRA references are read directly
from the selected store. Large weights and raw secrets are inventoried by path, size and change
time, checked again before reopening traffic, and retained in place; they are not copied into
the database or recovery archive. Moving openWeightGym processes or their file-backed assets to another
host remains a separate installation operation. The PostgreSQL database server may move without
moving those processes or artifacts.

#### Reports and interrupted jobs

The job runs as a transient `suite-db-migration-ID.service` through `systemd-run --user`, outside
WeightRoom's process. It survives stopping the console. System → DB shows durable reports from
WeightRoom's configured `data/migrations/`; the page is unavailable during the stop/cutover
window and can be reopened after services return. Verified recovery archives use the existing
configured backup root and the same recovery ID on each host, with retention disabled for this
rollback point. Private configuration snapshots are under each installation's
`data/migrations/ID/configuration.json`. The source is never deleted automatically.

A killed worker or host failure retains the maintenance marker and queued lease. Stop the
worker if it is still active, then run on the coordinator:

```bash
.venv/bin/python -m db.migration --recover /absolute/data/migrations/ID.job.json
```

Recovery stops participating services, restores staged settings, starts the original services,
checks acceptance, then releases the markers and lease. An already accepted installation is
released without switching back, because some clients may already have resumed writing.
An unsuccessful recovery retains the barrier and requires operator investigation. Do not delete
`settings/.suite-maintenance.json` or `pending.json` to force traffic open after a failed cutover.

Local verification includes both engine directions against disposable PostgreSQL 16, preserved
identity/password/key and artifact records, audit-chain continuation, concurrent PostgreSQL event
writers, BLOB/BYTEA payloads, deleted-ID sequence watermarks, schema/target refusal, private wizard
permissions and origin checks, paired-backup gating, partial-switch recovery, failed acceptance,
failed rollback, stale approval and interrupted-worker recovery. This is local evidence, not a
production engine cutover or live smoke-check claim.

### P5 - operational PostgreSQL support

Implemented locally on **2026-10-13**; PostgreSQL 18.6 health/tooling qualification passed on **2026-10-16**. The existing
System → DB page shows named-store reachability, engine, machine, repository
bindings, owning services (including turnstile for plugin records), probe duration and last recorded
verified local backup age. `GET /loadcoach/databases/health` exposes compute's lightweight
signals to accounts with `manage-settings`; the full inventory also includes these signals.
The storage panel sits directly below the Databases, Tables and On disk now summary cards.
The Machine page keeps its existing service states without storage probes. A missing Spotter
leaves the DB panel usable when authentication remains available.

`db/health.py` probes SQLite through a read-only connection without creating a missing file.
PostgreSQL probes use a read-only transaction, a two-second connect timeout and a two-second
statement timeout. The panel shows server version, database backends against the cluster client limit,
reserved slots, TLS on this connection and autovacuum. It warns at 80% of non-reserved connection
slots, when autovacuum is off, or when the server defaults to read-only transactions or is a standby.
The backend count uses public `pg_stat_database` totals across the cluster and includes
database-connected background workers. It is a conservative pressure signal against client
slots, not an exact client-slot count; no statistics role or access to other users' SQL is required.
Failures are isolated per store and return safe diagnostic text, never raw driver exceptions.
These checks establish connectivity and read access, not write permission, full integrity,
replication health, or host filesystem capacity. They do not count tables, run native backup
tools, wake compute, or change PostgreSQL settings.

Backup signals select schema-2 manifests with verified machine and store evidence, a present
nonempty local archive, an aware non-future timestamp, and matching store name, engine, source
display and current bindings. Fresh means at most **24 hours** old; older evidence is stale.
PostgreSQL backups now retain a non-secret hash of the connected database name/OID and server
address/port. A changed server/database or older backup without that fingerprint requires a new
backup before freshness is claimed; an unreachable PostgreSQL identity yields unknown status.
The panel reports the recorded restore verification, without reopening or rehashing the archive.
It does not establish that both halves of a recovery set are verified; use Backups for that.
Equivalent absolute SQLite path spellings resolve to the same source for freshness; changing the physical path still invalidates it. Backup freshness and store reachability are separate signals. Unavailable compute status does
not hide front stores or imply that compute recovered. No recurring jobs are introduced.

Local verification covers missing/corrupt stores, unchanged SQLite application data, credential-safe
probe failures, failed/future/malformed/stale backup evidence, engine/source/binding mismatches,
PostgreSQL native restore evidence, an unprivileged login and read-only role warnings, permissions,
compute/Spotter outages and desktop/mobile Chromium layouts without horizontal overflow.
The earlier P4 SQLite/PostgreSQL round trips and native recovery contracts also pass.
The final full suite passed **555 tests** with PostgreSQL 16 and matching native tools enabled;
**8 final focused checks** also passed. One existing Starlette/httpx deprecation warning remains.
The first full run hit an existing mocked Spotter sleep-test timing failure; its targeted repeat
and the final full run passed. These results establish local implementation, not deployment.

#### Roles and grants

Use one login per independent store, shared only by its consistency-bound services. openWeightGym
constructors run versioned DDL migrations, and the configured PostgreSQL login also performs
native isolated restore verification. It therefore needs ownership of its database/application
schema and **CREATEDB**, plus connectivity to the `postgres` maintenance database. A DML-only
login cannot currently satisfy startup and verified recovery. There is no separate maintenance
credential setting. Do not grant superuser, role creation, replication, or access to unrelated
openWeightGym databases. A connection limit is a budget, not an appropriate universal default.

For a new dedicated database, an administrator can use this template, choosing a connection
budget based on the services that will share it:

```sql
CREATE ROLE suite_core LOGIN NOSUPERUSER NOCREATEROLE NOREPLICATION CREATEDB CONNECTION LIMIT 24;
-- In psql, set the password interactively with: \password suite_core
CREATE DATABASE suite_core OWNER suite_core;
REVOKE ALL ON DATABASE suite_core FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE suite_core TO suite_core;
GRANT CONNECT ON DATABASE postgres TO suite_core;
-- Reconnect to suite_core as the administrator before the schema grants:
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO suite_core;
```

On a pre-existing database, grant ownership deliberately for each openWeightGym table and sequence;
grants on `public` alone do not transfer object ownership. Future startup migrations need to
alter those objects, and event-chain writers also acquire a table lock. Keep normal tables and
sequences owned by the store login. Do not revoke CREATEDB while expecting the current native
restore-verification workflow to work. Bound its access using host authentication and database
grants, and ensure `suite_verify_*` temporary databases can be reached during verification.
The installer can provision a store login; review that login against this operational contract.
See PostgreSQL's [role attributes](https://www.postgresql.org/docs/16/role-attributes.html) and
[privileges](https://www.postgresql.org/docs/16/ddl-priv.html).

#### Network and TLS

Bind PostgreSQL only to loopback or a selected private interface, and firewall it to the openWeightGym
hosts that own the bound services. Configure `pg_hba.conf` for those source addresses, required
databases and store logins, using SCRAM password authentication. Include the maintenance and
temporary restore databases in the access policy. Never publish the server to the public Internet.
For TCP links without a separately authenticated encrypted tunnel, configure server TLS and
`hostssl` rules and use `sslmode=verify-full` with a trusted root certificate and matching host
name in each protected DSN file. `require` encrypts but does not provide the same hostname
verification. A private LAN address alone does not encrypt traffic. Unix sockets or an explicitly
managed encrypted private tunnel may use a different transport policy; the UI reports the
current connection's TLS state without inferring tunnel security.
See [server TLS](https://www.postgresql.org/docs/16/ssl-tcp.html) and
[client certificate verification](https://www.postgresql.org/docs/16/libpq-ssl.html).

#### Connections, vacuum and monitoring

Budget connections across all service processes and independent repository handles, plus the
console probes, native dump/restore workers, migrations and administrator access. `max_connections`
is cluster-wide; store role limits apply to their logins. Reserve emergency slots and avoid
raising the cluster limit blindly because it increases resource allocation. openWeightGym currently
opens synchronous DB-API connections without a pool. Transaction pooling is not a drop-in
replacement for its session advisory migration locks. Monitor connection pressure and tune the
budget before increasing application concurrency. See
[connection settings](https://www.postgresql.org/docs/16/runtime-config-connection.html).

Leave autovacuum enabled. Review `pg_stat_user_tables` for dead tuples and last vacuum/analyze
times, plus disk space, long transactions, lock waits and server errors outside openWeightGym's bounded
probe. Run `ANALYZE` after a bulk engine migration or restore; ordinary `VACUUM (ANALYZE)` can
reclaim dead tuples and refresh estimates. `VACUUM FULL` rewrites tables and takes an exclusive
lock, so schedule it as maintenance only after measuring the need and space reserve.
See [routine vacuuming](https://www.postgresql.org/docs/16/routine-vacuuming.html) and
[monitoring views](https://www.postgresql.org/docs/16/monitoring-stats.html).

#### Recovery objectives and upgrades

openWeightGym's P3 custom logical dumps restore a store to a verified snapshot. They do **not** provide
point-in-time recovery, archive WAL, restore PostgreSQL role definitions, or recover excluded raw
keys and model files. Choose an acceptable recovery-point objective, create paired recovery sets
accordingly, and copy protected archives to a separate failure domain under an explicit secret
policy. The 24-hour UI threshold does not schedule backups or promise that recovery objective.
Keep role/grant setup and excluded artifacts recoverable separately.

If a shorter recovery point is needed, operate physical base backups with continuous WAL
archiving using PostgreSQL or an independently configured backup system. Monitor archiving
failures and retention and rehearse a restore to an isolated cluster at a selected timestamp.
That cluster recovery remains outside openWeightGym's logical recovery UI. See
[continuous archiving and PITR](https://www.postgresql.org/docs/16/continuous-archiving.html).

Before an upgrade, save PostgreSQL configuration, grants and extensions and create a verified
paired openWeightGym recovery set. Test the intended server, psycopg and matching native client tools
against disposable stores first. Minor releases update the same major cluster using the
distribution's instructions; major releases require dump/restore or `pg_upgrade`, not merely
replacing binaries. Use `pg_upgrade --check` when choosing that path, keep an independent old
cluster backup, stop participating writers during the change, and retain rollback until
acceptance. Restore native dumps with compatible clients, run `ANALYZE`, check every bound
service and store, and repeat P4's sign-in/model/request/chat/benchmark/tool acceptance categories.
Use SQLite's existing recovery contract on SQLite-only installations; no PostgreSQL daemon or
client tools are required for them. See
[pg_upgrade](https://www.postgresql.org/docs/16/pgupgrade.html).

## DB operations desk

System → DB uses the selected operations desk: a compact database ledger, expandable table
inventories and an operations panel. Install matching code on Front and every configured
LoadCoach to enable the node-scoped API. Existing SQLite/PostgreSQL bindings are preserved;
this change does not migrate an engine or delete production records on installation.
The sample proposals remain available through `scripts/preview-database-designs.py`.

<!-- db-design-todo:start -->
- [x] **Fleet discovery:** enumerate the configured Turnstile machines, including retired and
  unreachable nodes; probe with bounded concurrency and timeouts without waking or substituting
  hosts. A missing node or store blocks a complete export. Explicit partial exports record omissions.
- [x] **Store identity:** resolve registry bindings and actual engines; deduplicate proven physical
  identities across services/nodes, retaining consumer routes and an export owner. Local SQLite
  identities include the host and file identity; PostgreSQL uses server/database identity, with a
  host/endpoint scope for loopback or Unix-socket connections. Unprovable aliases remain separate.
- [x] **Unbound files:** show discovered/unused SQLite files separately, excluding them from active
  totals, all-active-store exports and maintenance. Only existing regular files are discovered;
  unused registry fallbacks and unbound PostgreSQL configuration are not filesystem discoveries.
  A configured missing database is an error, not
  a reason to silently use an old file. Mixed PostgreSQL/SQLite alone is valid.
- [x] **Compact inventory:** one row per known physical database; load expensive exact table counts
  only on expansion. Keep owner, bindings, sanitized location, version, allocated size, indexes,
  SQLite main/WAL/SHM/reusable bytes, tool status, verified-backup age and migration reports.
  Counts are unknown or explicitly estimated until details load. Refresh preserves filters,
  selection, expansion and focus; completed maintenance invalidates cached details.
- [x] **Scoped downloads:** signed, authorized node-specific database/table dumps stream from the
  selected owner. SQL filenames and store/table identifiers come from trusted catalogs, never
  arbitrary caller paths. Table-only SQL carries a dependency warning.
- [x] **Bulk export jobs:** all or selected stores produce a private ZIP with one SQL file per
  database and a manifest of identities, engines, consumers, snapshot windows, sizes, hashes and
  outcomes. Dump all ignores filters. Jobs retain partial failures, support cancellation between
  transactions/stores, survive page refresh, and mark interrupted work without automatic replay.
- [x] **Approved purge policy:** only bound Spotter `readings.at` is eligible initially. Reviews
  freeze the exact timestamp keys strictly before the chosen date at 00:00 UTC, so newly inserted
  or backdated readings are excluded. Spotter's existing `keep_days` sweep may independently remove
  readings; receipts report actual deletions. Accounts, settings, chained logs, active requests,
  private content, benchmarks and migration ledgers remain protected. Other histories require an
  owning-repository retention policy before becoming eligible; no date column is inferred.
- [x] **Reviewed execution:** maintenance reviews expire after ten minutes and are owner-bound,
  single-use and tied to store identity/schema revision. Permissions, same-origin POST protection,
  confirmation, per-store leases, bounded transactions and audit outcomes are enforced. Changes
  already committed on one store are not presented as rolled back when another store fails.
- [x] **Recovery before maintenance:** create and independently verify an encrypted single-store
  backup through the existing recovery path before deletion or compaction. Configure age tools and
  public recovery recipients on each owning node first. Private recovery identities stay offline;
  browser links expose receipts, not recovery archives. Backups follow ordinary backup retention;
  single-store maintenance copies do not claim a complete paired or fleet recovery set.
- [x] **Separate disk reclamation:** SQLite checkpoints WAL and uses VACUUM, with lock/time limits
  and rewrite headroom. PostgreSQL routine VACUUM (ANALYZE) makes space reusable; VACUUM FULL uses
  exclusive locks and rewrites storage. Full reclamation requires observable free space on the
  actual server filesystem and its tablespaces; remote/unreadable server storage is refused.
  Report measured before/after bytes rather than promising DELETE shrinks files. References:
  [SQLite VACUUM](https://www.sqlite.org/lang_vacuum.html) and
  [PostgreSQL vacuuming](https://www.postgresql.org/docs/current/routine-vacuuming.html).
- [x] **Node-specific recovery evidence:** probe health and backups on every owning node and
  keep unknown/stale/missing distinct. Native node receipts use the configured machine identity.
  Existing Backups remains the explicitly paired Front/default-Compute workflow; independent
  worker maintenance evidence is available in the DB desk's Recent operations.
- [x] **Validation:** four real signed LoadCoach fixtures plus Front, a real mixed-engine fleet,
  SQL round-trip restores, omitted-node manifests, protected stores, UTC boundaries, replay/owner/
  expiry/schema checks, low disk, PostgreSQL lock refusal and measured SQLite/PostgreSQL
  reclamation. Shared-component/all-page browser checks cover both themes, narrow layouts,
  error/empty states and dynamic replacements.
<!-- db-design-todo:end -->

### Operating limits and recovery

SQL export input is limited to 2 GiB per archive, with a 64 MiB disk reserve and bounded dump
workers. Export smaller stores separately when the limit is reached. SQL downloads expire after
24 hours; operation receipts and review plans are private files under Front's `database-jobs`
and each node's `database-operations` (Front maintenance uses `database-maintenance`). Job
receipts expire after seven days and reviews after ten minutes. A purge review is limited to
one million telemetry keys per owning-node review; choose fewer stores or an earlier cutoff for larger
histories. The per-store backup receipt names the owning machine and encrypted recovery set.

Inspect failed/interrupted receipts before retrying. Cancellation stops remaining work after
the current bounded transaction; it does not undo committed batches. If a node stops answering
after execution was accepted, inspect that node's job rather than sending a replacement.
Use the documented encrypted-backup recovery procedure on its owning server for rollback.
Keep the private recovery identity outside both servers. Migration profiles retain their separate
coordinated offline cutover/rollback workflow.

To restore an individual SQL export, choose an empty disposable target of the recorded engine:
`sqlite3 restored.db < database.sql`, or `psql --dbname=TARGET --set=ON_ERROR_STOP=1 --file=database.sql`.
Verify the manifest hash and restored content before using it. A collection of independent SQL
snapshots is not a simultaneous cross-store recovery point and does not replace a coordinated,
verified recovery set. Re-enable the original store configuration if a deployment is rolled back;
this UI release introduces no schema migration or automatic database engine switch.

## Private document collections in the chat store (milestone 105)

Implemented locally **2026-10-15**. `weightroom.chat` migration 3 adds owner-private collection,
original-document and indexed-chunk tables in the existing chat database. Immutable front
account identities own them. Base64 originals and JSON vectors use portable TEXT columns;
SQLite/PostgreSQL transfer and the existing private recovery set include them. There is no
new database, binding, DSN or vector extension. Generation/revision checks make index publication
atomic against deletion, cancellation and embedding-space rebuild. Account deletion removes
active collections through front cleanup. Earlier inference logs and backups retain their
normal retention policy. See [the collection guide](collections.md) for delivery status.


## Private workflow history in the chat store (milestone 106)

Local **2026-10-15**. `weightroom.chat` migration 4 adds immutable prompt versions, fixed workflow
plans and per-attempt records in the same private store. Stable account identities own them;
configuration/source references and outputs are JSON TEXT with portable SQL state/epoch guards.
No credentials, extra store or shadow billing authority is added. Accepted prior output survives
later failure; interrupted work pauses rather than automatically replaying keys. Existing backups,
engine transfer and account cleanup include these tables. Legacy chat-only migration refuses
populated new private tables. Source deletion removes active collection data while accepted
workflow history follows its explicit retention/deletion controls; see [the guide](workflows.md).

## Advisory spending policies (milestone 107)

`loadcoach.users` migration 4 adds portable `spending_allowances` to the existing front
accounts store. Its scope/subject key holds optional positive USD amounts; there is no cost
ledger or reservation table. Existing ordinary event details retain account/key/group
attribution; full-store recovery/transfer preserves both. Legacy `accounts-from` refuses
populated policies rather than copying only old account tables. Matching front/compute
delivery status is recorded in [allowances](allowances.md).

## Scheduled machine-key rotation (milestone 136)

`loadcoach.users` migration 5 adds `machine_key_rotations` to the front accounts store: one
interval, immutable owner identity, next attempt and last error per configured compute node.
It stores no key material. Key files remain owner-only filesystem secrets and their modification
times remain the authoritative last-rotation record. Existing account-store backup, restore and
cross-engine transfer include the schedule table.


## Identity-only portability (milestone 152)

The offline `suite-identity` command projects only `groups`, `users`, `keys`,
`spending_allowances`, `services` and `settings_changed` from the front SQLite account store.
Import uses existing schema migrations and refuses different populated identity/configuration
state; it does not copy the database file or its histories. Raw credential files are carried
only in the separate encrypted host bundle. [Coverage, commands and interrupted-import recovery](operations.md#identity-and-credential-portability-milestone-152).
