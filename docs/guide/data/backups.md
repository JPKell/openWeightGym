<!--docs
title = "Backups and restore"
summary = "Recovery sets from both machines, what they leave out, and keeping identities through a reinstall."
order = 20
covers = ["/backups"]
-->
# Backups and restore

## Set up recovery encryption {#setup}

Every new recovery archive requires [age](https://age-encryption.org/) authenticated encryption. Servers hold public recipients; private recovery keys belong in separate recovery storage. Missing tools or recipients stop a backup before snapshots are taken. There is no unencrypted fallback.

- Install the distribution's `age` package on the control node, on every compute node and on a trusted recovery computer. It supplies `age` and `age-keygen`. The main installer includes this dependency; existing installations need it added.

- Generate the key pair on the recovery computer using the first block below. Keep `recovery.key` on encrypted removable storage or in a private credential vault, with a second independent recovery copy. Keep it off application servers, source control, email and logs.

- Copy only `recipients.txt` to every machine: the control node and each compute node. Each machine reads it from its own data folder, at `DATA/backups/recipients.txt`; [Where recipients.txt goes](#recipient-location) gives the full path for each kind of installation and the commands. The file accepts 1–20 native `age1…` public recipients, one per line; any corresponding private key can decrypt the archive.

- Check each machine with the readiness command below, then open [System → Backups](/backups), confirm the hosts are ready, create a set, and test it with the saved recovery key.

Shell

```text
umask 077
age-keygen -o recovery.key
age-keygen -y recovery.key > recipients.txt
```

**Keep the recovery key separate**

A lost private key cannot be reset or recovered by the server. Anyone obtaining it can decrypt retained archives addressed to it. A compromised service can still read live data, substitute future recipients or delete local backups.

## Where recipients.txt goes {#recipient-location}

Every machine backs up its own stores and encrypts them with the recipients in its own data folder. The control node and each compute node therefore each need their own copy, at `DATA/backups/recipients.txt`, where `DATA` is that machine's data folder.

The data folder is the `data` setting in the machine's settings file, counted from the folder that holds the settings file. The control node reads `settings/weightroom.toml`; a compute node reads `settings/loadcoach.toml`. Both are installed with `data = "../data"`, so the data folder is the `data` folder beside `settings` in the installation folder. A copy anywhere else, such as `settings/recipients.txt`, is not read.

| Installation | Installation folder | recipients.txt | Owned by |
| --- | --- | --- | --- |
| For one account (the default) | `/home/ACCOUNT/openWeightGym` | `/home/ACCOUNT/openWeightGym/data/backups/recipients.txt` | The account that runs the services (the one that ran the installer) |
| For one account, another folder (`--dir PATH` or `OWG_HOME`) | `PATH` | `PATH/data/backups/recipients.txt` | The account that runs the services |
| Whole machine (`--system`) | `/opt/openweightgym` | `/var/lib/openweightgym/data/backups/recipients.txt` (`/opt/openweightgym/data` links there) | The `openweightgym` account |
| A changed `data` setting | Any | `SETTINGS/DATA/backups/recipients.txt`, where `SETTINGS` is the folder of the settings file and `DATA` the setting's value (an absolute `DATA` is used as it is) | The account that runs the services |

To confirm a machine's data folder, read the setting: `grep '^data' /home/ACCOUNT/openWeightGym/settings/weightroom.toml` on the control node, or `…/settings/loadcoach.toml` on a compute node.

The file and its folder are checked before every backup. The `backups` folder must belong to the account that runs the services and is kept at mode 700. `recipients.txt` must be a regular file (not a link) owned by that account or root, and not writable by its group or others; use mode 600. Otherwise the backup stops with "Backup key/configuration permissions are unsafe".

### For one account, on the machine itself

Run as the account that runs the services, with `recipients.txt` copied to the current folder:

Shell

```text
install -d -m 700 /home/ACCOUNT/openWeightGym/data/backups
install -m 600 recipients.txt /home/ACCOUNT/openWeightGym/data/backups/recipients.txt
```

### For one account, from the recovery computer over SSH

Repeat for the control node and each compute node, replacing `ACCOUNT@MACHINE`. Only the public file leaves the recovery computer.

Shell

```text
ssh ACCOUNT@MACHINE 'umask 077 && install -d -m 700 /home/ACCOUNT/openWeightGym/data/backups && cat > /home/ACCOUNT/openWeightGym/data/backups/recipients.txt' < recipients.txt
```

### Whole-machine installation

The `openweightgym` account cannot sign in, so an administrator installs the file for it:

Shell

```text
sudo install -d -m 700 -o openweightgym /var/lib/openweightgym/data/backups
sudo install -m 600 -o openweightgym recipients.txt /var/lib/openweightgym/data/backups/recipients.txt
```

### Check that a machine is ready

This runs the same check a backup runs first, without making a backup or reading the stores. Run it as the account that runs the services (for a whole-machine installation, prefix it with `sudo -u openweightgym` and use `/opt/openweightgym` and `/var/lib/openweightgym/data`). It prints `'ready': True` when `age`, `age-keygen` and the recipients are all usable; otherwise the reason.

Shell

```text
cd /home/ACCOUNT/openWeightGym && .venv/bin/python -c 'from pathlib import Path; from loadcoach.backup_crypto import encryption_status; print(encryption_status(Path("/home/ACCOUNT/openWeightGym/data/backups")))'
```

**Changing recipients**

Replace the file on every machine together, then make and test a new set. Backups already made stay addressed to the recipients in force when they were made.

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

## Older archives and key rotation {#legacy}

Existing `archive.tar.gz` files remain plaintext. They are labeled `legacy-unencrypted`, excluded from protected recovery points and preserved outside automatic encrypted retention. New encryption does not protect old copies or filesystem snapshots.

Create and test an encrypted replacement before retiring an old recovery point. If historical data must remain, encrypt its old archive with age on a trusted machine, test decryption, and retain its original manifest. The offline tool accepts its embedded legacy schema, but manually wrapping an archive does not upgrade its server verification record. Remove plaintext copies and address snapshots under your storage policy after proving recovery.

For key rotation, generate a new private key off-server, replace public recipients on each host and test a new pair. Keep previous private keys separately while archives addressed to them exist. If a key is compromised, restrict access and re-encrypt retained data for a new key; rotation cannot protect ciphertext already stolen.
