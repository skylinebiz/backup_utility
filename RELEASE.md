# Release Notes

## v1.3.0 - 2026-09-18

### Highlights

- **Manual backups on demand.** A new **Start Backup Now** button sits in the page toolbar next to Save. It runs the same backup process used by the schedule, keeps a full log entry like any other run, and greys itself out whenever a backup is already in progress so you can't kick off two at once.
- **Schedules now survive `bench migrate`.** Previously, migrating the site would silently delete the scheduled backup job, because Frappe removes any `Scheduled Job Type` that isn't declared in an app's `hooks.py`. The schedule is now automatically re-created from your saved settings right after every migrate.
- **Test Connection is reliable again.** Two bugs are fixed:
  - It used to validate whatever FTP credentials were last *saved*, not what you'd just typed in the form — so testing new credentials before the first save (or after changing an existing password) could fail even though what you typed was correct.
  - After saving and reloading the page, the password field is sent back to the browser masked with asterisks. The check for "this is just the mask, not a new password" only worked correctly for 5-character passwords; anything else could get silently overwritten with the mask itself, breaking the connection until you re-typed the password. This is now handled correctly for passwords of any length.
- **Manual backups no longer require the schedule to be enabled.** The **Enabled** checkbox now only controls the automatic daily schedule — **Start Backup Now** works regardless of whether it's checked.

### Upgrade notes

- No data migration needed. After updating, run `bench migrate` once as usual — your existing backup schedule will be preserved automatically going forward.
- If your FTP **Test Connection** was failing after a page reload, it was very likely this release's password-masking bug, not your credentials. Try again after upgrading.

### Fixes included

- Scheduled backup job deleted on `bench migrate` — now restored via an `after_migrate` hook.
- Test Connection validating stale saved credentials instead of the current form values.
- Test Connection mis-detecting a masked saved password as a literal new password for passwords other than 5 characters.
- Manual backup trigger incorrectly requiring **Enabled** to be checked.

---

## v1.2.0 - 2026-08-19

- Added **Test Connection** button and whitelisted method to verify FTPS credentials before saving.
- Save is now blocked while **Upload?** is enabled until the connection has been tested successfully, and re-blocked if the FTP settings change afterwards.

## v1.1.0 - 2026-08-18

- Added FTPS (FTP with TLS) support for uploads, replacing plain FTP.
- Updated the scheduler job method used for the daily backup schedule.

## v1.0.0 - 2026-08-17

- Initial release: **Backup Utility** settings doctype and **Backup Log** audit trail.
- Dynamically managed cron-based `Scheduled Job Type`, driven by the **When** field.
- Local backup via `bench backup`, with an optional `--with-files` toggle.
