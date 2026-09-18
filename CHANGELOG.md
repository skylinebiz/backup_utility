# Changelog

All notable changes to this project are documented in this file.

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
