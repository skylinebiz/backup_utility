# Backup Utility

A [Frappe](https://frappeframework.com/) app that schedules automated site backups and uploads them to a remote server over **FTPS** (FTP with TLS). It keeps a full audit trail of every backup/upload run and can automatically prune old local backups once they exceed a configured size.

## Features

- **Scheduled daily backups** at a time you configure, driven by a dynamically managed cron-based `Scheduled Job Type` (no manual cron setup required).
- **Manual/on-demand backup** via a whitelisted API method.
- **Database + optional files backup** using the standard `bench backup` command (`--with-files` toggle).
- **Secure FTP upload (FTPS)** of newly created backup files to a remote host, with automatic remote directory creation.
- **Optional local cleanup** — delete the local copy once it's been uploaded successfully.
- **Local backup retention** — cap total local backup storage (in MB); oldest files are deleted first once the limit is exceeded.
- **Per-run audit log** (`Backup Log`) with a timestamped process log, backup status, and upload status.
- **At-a-glance status** on the settings page — last backup time/status/error and last upload time/status/error.

## How it works

### DocTypes

- **Backup Utility** (single) — the configuration screen:
  - `Enabled` / `When` — turns scheduling on and sets the daily backup time.
  - `Include Files?` — adds `--with-files` to the backup command.
  - `Maximum Backup Size (MB)` — total local backup storage cap; oldest files are purged first when exceeded (leave empty/0 to disable cleanup).
  - **Upload Config** — `Upload?`, `Host`, `Port`, `Username`, `Password`, `Path` (remote directory, created automatically if missing), `Delete Local Backup after Upload`.
  - **Latest Status** — read-only `last_backup_at/status/error` and `last_upload_at/status/error`, plus a link to the most recent `Backup Log`.
- **Backup Log** — one record per backup run, auto-named `BL-YY-MM-DD-####`:
  - `Process` — `Local`, `Local and Upload`, or `Local, Upload and Delete`.
  - Backup status/time/error, upload status/time/error, and a running `Process Log` with timestamped entries for each step.

### Scheduling

Saving the **Backup Utility** doc (`on_update`) creates or updates a `Scheduled Job Type` for `backup_utility.api.backup.ftp_backup_cron`, using a cron expression built from the `When` time (`minute hour * * *` — i.e. once a day). Disabling the utility or clearing the time stops the job instead of deleting it. When the cron fires, it enqueues `execute_backup` on the `long` queue.

### Backup process (`run_backup`)

1. Snapshots existing files in the site's `private/backups` directory, then runs:
   ```bash
   bench --site <site> backup [--with-files] --backup-path <private/backups>
   ```
2. Diffs the directory to find only the files this run created (`.json`, `.sql`, `.sql.gz`, `.tar`, `.tar.gz`, `.tgz`).
3. Updates the `Backup Log` and the `Backup Utility` singleton with the result.
4. If **Upload?** is enabled, connects to the configured host over `ftplib.FTP_TLS`, authenticates, switches to secure data protection (`PROT P`), changes/creates the remote path, and uploads each new file. On success (and if **Delete Local Backup after Upload** is set), the local file is removed.
5. If a **Maximum Backup Size** is configured, deletes the oldest local backup files until total size is back under the limit.

Every step is appended to the `Backup Log`'s `Process Log`, and key events are also written to Frappe's error log for visibility in the Frappe Cloud / bench log viewer.

### Manual trigger

`backup_utility.api.backup.execute_backup` is a whitelisted method that runs the same backup process synchronously and returns `{"success": true}` — call it from the client, a server script, or `bench execute` to trigger a backup on demand.

## Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch main
bench install-app backup_utility
```

## Setup

1. Open **Backup Utility** in the desk.
2. Check **Enabled** and set **When** to the desired daily backup time.
3. Optionally check **Include Files?** to back up the files directory alongside the database.
4. Optionally set **Maximum Backup Size (MB)** to cap local backup storage.
5. To upload backups off-site, check **Upload?** and fill in **Host**, **Port**, **Username**, **Password**, and **Path**. Check **Delete Local Backup after Upload** if you don't want to keep a local copy once it's safely uploaded.
6. Save — the scheduled job is created/updated automatically. Progress and history can be reviewed under **Backup Log**.

## Requirements

- Frappe / bench (`~16.0`)
- Python 3.14+
- An FTP server that supports **FTPS** (explicit TLS via `AUTH TLS`); plain FTP is not supported.

## Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/backup_utility
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

## License

mit
