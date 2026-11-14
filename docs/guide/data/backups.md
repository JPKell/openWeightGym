<!--docs
title = "Backups and restore"
summary = "Create node-owned recovery sets, understand exclusions and find restore procedures."
order = 20
covers = ["/backups"]
-->
# Backups and restore

Create encrypted recovery sets and inspect each owning node's readiness.

## Set up recovery encryption {#setup}

Follow [Set up recovery encryption](/docs/data/backup-encryption#setup) for the complete procedure.

## Where recipients.txt goes {#recipient-location}

Follow [Where recipients.txt goes](/docs/data/backup-encryption#recipient-location) for the complete procedure.

## Create and retain backups {#recovery-sets}

[System → Backups](/backups) creates a shared set ID for the control node and compute. Scheduled backups use the same encrypted path. Each host snapshots every configured store: SQLite uses its online backup API; PostgreSQL uses `pg_dump`, a disposable restore, and content/schema comparisons. Missing or unreachable required stores fail that host's backup.

Compressed archive bytes stream directly into age. Before publication, the host decrypts the actual ciphertext with a disposable verification key, safely extracts it, and checks the embedded inventory, file hashes, SQLite integrity and counts. The temporary private key stays in memory and is discarded; only the encrypted archive and an owner-only sidecar manifest are published under `data/backups/ID/`. Creation and rotation are serialized locally.

**Verified means the local creation-time check passed.** It does not prove your saved private key works, that an offsite copy exists, or that the application has passed a recovery drill. Both halves must pass for a verified pair. Separate stores and hosts are not one distributed transaction; drain work and stop all writers before an offline backup if you need a coordinated snapshot.

Retention keeps 0–100 encrypted sets per host; zero keeps all. Rotation follows successful local publication. A partial pair does not stop retention advancing on the successful host, so allow enough sets for outages. External copies and legacy plaintext archives are managed separately.

Keep three copies across two independent storage systems, with one offline or immutable offsite copy. Copy both encrypted halves and their manifests to storage the application account cannot modify or delete, and save ciphertext hashes independently. Schedule backups for the data loss you can tolerate; test full recovery monthly and after key/storage changes. See [CISA's encrypted, offline backup guidance](https://www.cisa.gov/sites/default/files/publications/CISA_MS-ISAC_Ransomware%20Guide_S508C.pdf).

**Temporary plaintext and metadata**

Snapshots and verification files exist briefly inside private temporary directories. Use encrypted host/recovery disks and encrypted swap. Normal completion and handled errors remove temporary files; interruption may require administrator cleanup. Deletion does not guarantee secure erasure on SSDs or snapshots. The sidecar contains paths, hashes and counts, not raw credentials; it is operational metadata, not independent proof of provenance. The full manifest is also encrypted inside the archive.

## Protected data and exclusions {#protected-data}

| Data | Recovery handling |
| --- | --- |
| Configured account/application stores | Encrypted, including password verifiers, authenticator seeds, permissions, conversations, requests and durable job grants. Treat archives as credential-bearing private data. |
| Settings and saved image/audio assets | Included. Recognized inline secret fields in ordinary settings are refused. Secrets pasted into histories or other retained data remain part of those stores. |
| Separate key folders, environment files, runtime CLI tokens and cookie-signing secret | Excluded from normal recovery sets. Recover long-lived API/provider/service keys from a separate private source. Services regenerate runtime tokens; users sign in again. |
| Model weights | Excluded. Compute records stable identities, paths, sizes, known hashes and inferred Hugging Face sources. Recover and verify weights separately. |

Archives have no browser download endpoint. Manifest viewing and backup creation require `manage-settings`. Files are mode 0600 beneath mode 0700 directories; permissions do not isolate processes using the same service account or protect against root.

## Verify and restore {#restore}

Follow [Verify and restore](/docs/data/restore#restore) for the complete procedure.

## Planned reinstall {#identities}

Follow [Planned reinstall](/docs/data/restore#identities) for the complete procedure.

## Older archives and key rotation {#legacy}

Existing `archive.tar.gz` files remain plaintext. They are labeled `legacy-unencrypted`, excluded from protected recovery points and preserved outside automatic encrypted retention. New encryption does not protect old copies or filesystem snapshots.

Create and test an encrypted replacement before retiring an old recovery point. If historical data must remain, encrypt its old archive with age on a trusted machine, test decryption, and retain its original manifest. The offline tool accepts its embedded legacy schema, but manually wrapping an archive does not upgrade its server verification record. Remove plaintext copies and address snapshots under your storage policy after proving recovery.

For key rotation, generate a new private key off-server, replace public recipients on each host and test a new pair. Keep previous private keys separately while archives addressed to them exist. If a key is compromised, restrict access and re-encrypt retained data for a new key; rotation cannot protect ciphertext already stolen.
