<!--docs
title = "Verify and restore"
summary = "Verify ciphertext and stage an isolated recovery before replacing live stores."
order = 22
covers = []
-->
# Verify and restore

Test a recovery archive with the separately held private key before applying it.

## Verify and restore {#restore}

- Choose a complete pair from before the failure or suspected compromise. Copy each host's `archive.tar.gz.age` to a trusted recovery computer with enough encrypted disk space. Obtain the matching private key separately. Supply `--sha256` with the ciphertext hash from an independent trusted record when available.

- Run the verification command once per half. It authenticates the complete ciphertext before extraction and validates the embedded manifest and payload hashes. It rejects traversal, links, special files, duplicates and size-limit violations. It changes no live stores and removes temporary plaintext. The archive/extraction limit defaults to 1 TiB; adjust `--max-bytes` deliberately for larger installations.

- Add `--output` to stage files in a new private directory; existing directories are refused. Inspect its embedded `manifest.json`, original destinations, store engines and services to stop. Anyone holding a public recipient can create a new archive: use trusted backup storage and an independent ciphertext hash to establish provenance.

- For a drill, restore into an isolated installation with compatible application/schema versions. Restore PostgreSQL dumps into a fresh disposable database using compatible `pg_restore`, then check schemas and contents. The verifier checks dump hashes; it does not perform a new PostgreSQL server restore.

- For live recovery, drain work and stop every affected writer, worker, adapter, schedule and automatic restart. Preserve current databases and matching SQLite WAL/SHM files privately for rollback. Install only staged files mapped to reviewed destinations, with correct service ownership and mode 0600. Never combine restored SQLite files with old WAL/SHM files. Restore PostgreSQL into a fresh target and switch all owners together; never overwrite an active database.

- Recover long-lived credentials and model files separately. Start services and test password/authenticator sign-in, permissions, model inventory, conversations/media and benchmark evidence. Review restored grants and pending jobs before resuming schedules. Record the recovery-key and application checks in your existing operations record.

Shell

```text
python -m loadcoach.backup_restore /recovery/front/archive.tar.gz.age \
  --identity /private/recovery.key
```

Shell

```text
python -m loadcoach.backup_restore /recovery/front/archive.tar.gz.age \
  --identity /private/recovery.key --output /recovery/front-staged
```

The installed `suite-backup-verify` command has the same interface. Its JSON report identifies the set, machine, ciphertext hash and whether PostgreSQL restore testing remains. A recovery-key test does not change the server's historical manifest.

**After a compromise**

Rebuild affected hosts from trusted software and use a known-clean backup. Before reconnecting, revoke restored API/job tokens, replace service/provider keys, reset affected passwords and re-enroll authenticators. Old backups can restore credentials or permissions revoked since they were created.

## Planned reinstall {#identities}

`suite-identity` creates a separate bundle of accounts, permissions, password verifiers, authenticator seeds, service credentials and configuration, excluding request/conversation history. It uses AES-256-GCM with a scrypt-derived key. Treat it as a master credential backup: use a unique randomly generated passphrase, store that separately, and keep the bundle offline or in immutable private storage.

Stop participating services, adapters, schedules and automatic restarts before export or applying an import. Imports preview first and refuse different existing accounts. This tool currently requires SQLite accounts and original installation paths. Test an isolated import before reinstalling. Use it for planned continuity; replace credentials after a compromise. See the [identity export/import reference](/docs/code/accounts?module=loadcoach/identity_bundle.py#source-title) for commands and constraints.
