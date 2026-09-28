# Changelog

All notable changes to this project are documented in this file.

## [2.1.0] - 2026-09-28

### Added

- **Backup Retention (Days)** field (0 = disabled). After every upload attempt, backups older than this on the remote storage (S3 / FTPS) are deleted - checked against whatever is already there, not just the files just uploaded. Only touches this app's own backup file types, within the configured Path / Prefix.

### Notes

- Retention is remote-only: it never deletes local backup files. The local copy is still removed only by **Delete Local Backup after Upload** (right after that file's own successful upload) or by **Maximum Backup Size (MB)** cleanup - unrelated to this setting.

## [2.0.0] - 2026-09-21

### Added

- **S3-compatible object storage uploads** (AWS S3, Cloudflare R2, Backblaze B2, MinIO, ...) as an alternative to FTPS, chosen via the new **Configuration Type** field with its own Bucket, Endpoint URL, Region, Access Key ID, Secret Access Key and Path / Prefix fields. Implemented with the Python standard library only (a minimal AWS Signature V4 client), so there is no new dependency. Large backup files are streamed rather than loaded into memory, and a bucket name pasted into the Endpoint URL is ignored instead of causing a 404.
- **Test Connection** now works for both FTPS and S3, and tests whichever **Configuration Type** is currently selected in the form, even if unsaved.

### Changed

- **Start Backup Now** now runs through the backup schedule's own `Scheduled Job Type` (`enqueue(force=True)`), the same path Frappe's scheduler uses, instead of running synchronously in the request. It is queued rather than run immediately, so on a busy queue it may wait its turn, and it shows "queued" instead of a final success/failure. `Enabled` only gates the automatic daily schedule, so a manual run works whether or not it is checked.
- The backup schedule record is now always created (stopped while disabled or without a time) so **Start Backup Now** works from the first save.
- Backup Log entries say "Upload" instead of "FTP upload", since the backend can now be S3.

### Fixed

- The backup schedule was deleted by `bench migrate`. It is now linked to a Frappe `Scheduler Event`, which migrate leaves alone. The first migrate after upgrading an existing site recreates the job once (new record name, same schedule), and it persists from then on.

### Upgrade notes

- Run `bench migrate` after updating to add the new S3 fields. Existing FTPS setups keep working: sites with no Configuration Type saved are treated as FTPS.

## [1.3.0] - 2026-09-18

### Added

- **Start Backup Now** button in the page toolbar for on-demand manual backups, disabled while a backup is already running (`backup_utility.api.backup.get_backup_status`).

### Fixed

- The scheduled backup job was silently deleted every `bench migrate` (Frappe prunes any `Scheduled Job Type` not declared in `hooks.scheduler_events`, and this app manages its schedule dynamically). An `after_migrate` hook now re-creates it from the saved settings.
- **Test Connection** tested stale/saved FTP credentials instead of whatever was currently typed in the form, since saving is blocked until the test succeeds. The client now sends the live form values.
- **Test Connection** could fail after a save + page reload: a saved Password field is sent back to the client masked as asterisks matching the real password's length (not a fixed `"*****"`), and only passwords exactly 5 characters long were being detected as "unchanged" correctly.
- A manual backup via `execute_backup` required **Enabled** (the automatic daily schedule toggle) to be checked; manual and scheduled triggers are now independent.

## [1.2.0] - 2026-08-19

### Added

- **Test Connection** button and whitelisted method to verify FTPS credentials before saving.
- Save is now blocked while **Upload?** is enabled until the connection has been tested successfully, and re-blocked if the FTP settings change afterwards.

## [1.1.0] - 2026-08-18

### Added

- FTPS (FTP with TLS) support for uploads, replacing plain FTP.

### Changed

- Updated the scheduler job method used for the daily backup schedule.

## [1.0.0] - 2026-08-17

### Added

- Initial release: **Backup Utility** settings doctype and **Backup Log** audit trail.
- Dynamically managed cron-based `Scheduled Job Type`, driven by the **When** field.
- Local backup via `bench backup`, with an optional `--with-files` toggle.
