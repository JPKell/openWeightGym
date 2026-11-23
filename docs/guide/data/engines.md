<!--docs
title = "SQLite and PostgreSQL"
summary = "The two database engines, and moving a store from one to the other."
order = 30
covers = ["/databases"]
-->
# SQLite and PostgreSQL

## One storage layer, two engines {#engines}

The shared `db/` package makes connections and transactions, binds each service's repositories to a named store, runs each store's numbered schema migrations, and backs up and restores either engine. Different machines, services and repositories can use different stores and engines.

SQLite is the default, and existing SQLite files are upgraded in place by the same migrations. PostgreSQL stores need the native `pg_dump` and `pg_restore` programs, of a compatible version, on the machine that owns the store; [System → DB](/databases) says whether they are there. Credentials are read from an owner-only connection file and never written to manifests or errors.

## Moving a store {#migration}

The installer will not switch a populated store to another engine. Moving one is a separate, offline maintenance step: an operator writes an owner-only migration profile under `settings/migrations/`, prepares the empty target and its acceptance checks, and installs the same openWeightGym code on every host. System → DB then previews the profile's target and bindings, and starts it only after that preview.

A migration copies one named store while its services are stopped, checks the copy, and changes only that store's bindings. If the acceptance checks fail, it rolls back to the original store.

## Export and maintenance

**Dump all** reviews every active database, regardless of filters. If a node is unavailable,
**Start partial export** requires two clicks and records omissions in the manifest. **Dump
selected** is complete once every chosen database is exported, whatever other nodes are doing. Exports
contain separate per-database snapshots, not a simultaneous cluster recovery set.

For purge or disk reclaim, choose stores and preview the operation first. Review the counts,
cutoff, locks and space requirements, then click **Apply reviewed operation** twice.
Changing the cutoff or reclaim method invalidates the preview. A successful retry clears the previous error. If full reclamation cannot inspect filesystem space or permissions, use routine maintenance for space reuse or ask the database administrator to check storage access; routine maintenance does not promise a smaller file. The server rechecks the review
and creates a verified recovery copy before changing each store; completed work cannot be
cancelled or rolled back automatically. SQL exports may contain secrets; keep them private.
