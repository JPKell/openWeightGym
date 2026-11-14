<!--docs
title = "Recovery encryption"
summary = "Prepare public recipients on each owning node and keep recovery keys separate."
order = 21
covers = []
-->
# Recovery encryption

Prepare encryption before creating a recovery archive.

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
