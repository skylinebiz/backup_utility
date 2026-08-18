
import frappe
import ftplib
import os
import subprocess
import time
from pathlib import Path
from datetime import datetime

from frappe import _
from frappe.utils import cint, now_datetime, get_time, flt

logger = frappe.logger("backup_utility")


def ftp_backup_cron():

    doc = get_backup_utility()

    if not cint(doc.enabled):
        logger.info(
            "Backup Utility - Scheduler skipped: Backup Utility is disabled."
        )
        return

    if not doc.when:
        logger.info(
            "Backup Utility - Scheduler skipped: no backup time configured."
        )
        return

    logger.info(
        f"Backup Utility - Scheduler triggered for site {frappe.local.site} "
        f"(configured time: {doc.when}). Queueing execute_backup on long queue."
    )

    job = frappe.enqueue(
        "backup_utility.api.backup.execute_backup",
        queue="long",
        timeout=3600,
        job_name=(
            f"backup_utility_scheduled_backup:"
            f"{frappe.local.site}"
        ),
        at_front=True,
    )

    logger.info(
        f"Backup Utility - Backup queued for site {frappe.local.site} "
        f"(job id: {job.id if job else 'UNKNOWN'})."
    )


def get_backup_utility():
    return frappe.get_single("Backup Utility")


def get_backup_directory():
    backup_directory = frappe.get_site_path("private", "backups")

    os.makedirs(backup_directory, exist_ok=True)

    return os.path.abspath(backup_directory)


# Concurrency Lock
#
# Prevents overlapping runs (e.g. a manual trigger while the scheduled
# job is still running) from racing on the before/after file diff,
# double-uploading files, or corrupting cleanup accounting.

BACKUP_LOCK_FILENAME = ".backup_utility.lock"
BACKUP_LOCK_STALE_SECONDS = 2 * 60 * 60  # well beyond the 3600s backup timeout


def get_backup_lock_path(backup_directory):
    return Path(backup_directory) / BACKUP_LOCK_FILENAME


def acquire_backup_lock(backup_directory):

    lock_path = get_backup_lock_path(backup_directory)

    if lock_path.exists():

        age = time.time() - lock_path.stat().st_mtime

        if age > BACKUP_LOCK_STALE_SECONDS:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
        else:
            return False

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False


def release_backup_lock(backup_directory):
    try:
        get_backup_lock_path(backup_directory).unlink()
    except FileNotFoundError:
        pass


# Backup Files

def get_backup_files(backup_directory):

    backup_directory = Path(backup_directory)

    if not backup_directory.exists():
        return set()

    allowed_suffixes = (
        ".json",
        ".sql",
        ".sql.gz",
        ".tar",
        ".tar.gz",
        ".tgz",
    )

    return {
        file_path
        for file_path in backup_directory.iterdir()
        if (
            file_path.is_file()
            and file_path.name.endswith(allowed_suffixes)
        )
    }


# Upload Markers
#
# A backup file that fails to upload is retained locally and marked as
# "pending" so size-based cleanup never deletes the only copy of a
# backup that never made it off-site.

UPLOAD_PENDING_SUFFIX = ".upload_pending"


def get_upload_marker_path(backup_file):
    return backup_file.parent / f"{backup_file.name}{UPLOAD_PENDING_SUFFIX}"


def mark_upload_pending(backup_file):
    try:
        get_upload_marker_path(backup_file).touch(exist_ok=True)
    except Exception:
        pass


def clear_upload_marker(backup_file):
    try:
        get_upload_marker_path(backup_file).unlink()
    except FileNotFoundError:
        pass


def prune_orphan_upload_markers(backup_directory):

    backup_directory = Path(backup_directory)

    if not backup_directory.exists():
        return

    for marker in backup_directory.glob(f"*{UPLOAD_PENDING_SUFFIX}"):

        target = marker.with_name(
            marker.name[: -len(UPLOAD_PENDING_SUFFIX)]
        )

        if not target.exists():
            try:
                marker.unlink()
            except FileNotFoundError:
                pass


# Local Backup Cleanup

def cleanup_old_backups(
    backup_directory,
    max_size_mb,
    log,
):

    # If it is empty or 0, do not delete any old backups.
    if not max_size_mb:
        append_process_log(
            log,
            "Maximum backup size not configured. "
            "Old backup cleanup skipped."
        )
        return

    max_size_mb = flt(max_size_mb)

    if max_size_mb <= 0:
        append_process_log(
            log,
            "Maximum backup size is 0 or less. "
            "Old backup cleanup skipped."
        )
        return

    max_size_bytes = max_size_mb * 1024 * 1024

    prune_orphan_upload_markers(backup_directory)

    all_files = list(
        get_backup_files(backup_directory)
    )

    total_size = sum(
        path.stat().st_size
        for path in all_files
        if path.exists()
    )

    append_process_log(
        log,
        f"Current local backup size: "
        f"{total_size / (1024 * 1024):.2f} MB. "
        f"Maximum allowed: {max_size_mb:.2f} MB."
    )

    # Files still awaiting a successful upload are never deletable,
    # even if they are the oldest files on disk.
    deletable_files = [
        path for path in all_files
        if not get_upload_marker_path(path).exists()
    ]

    deletable_files.sort(
        key=lambda path: path.stat().st_mtime
    )

    while total_size > max_size_bytes and deletable_files:

        oldest = deletable_files.pop(0)

        try:

            file_size = oldest.stat().st_size
            oldest.unlink()
            clear_upload_marker(oldest)
            total_size -= file_size

            append_process_log(
                log,
                f"Deleted oldest local backup: "
                f"{oldest.name}"
            )

        except FileNotFoundError:
            continue

        except Exception as exc:

            append_process_log(
                log,
                f"Failed to delete old backup "
                f"{oldest.name}: {exc}"
            )

            frappe.log_error(
                frappe.get_traceback(),
                "Backup Utility - Cleanup Failed"
            )

    if total_size > max_size_bytes:
        append_process_log(
            log,
            "Local backup size still exceeds the configured maximum, "
            "but the remaining files are pending FTP upload and were "
            "not deleted."
        )

    append_process_log(
        log,
        f"Local backup cleanup completed. "
        f"Current size: "
        f"{total_size / (1024 * 1024):.2f} MB"
    )


# FTP

def ftp_change_directory(ftp, remote_path):

    remote_path = (remote_path or "/").strip()

    if not remote_path or remote_path == "/":
        return

    parts = remote_path.strip("/").split("/")

    ftp.cwd("/")

    for part in parts:

        try:
            ftp.cwd(part)

        except ftplib.error_perm:
            ftp.mkd(part)
            ftp.cwd(part)


def upload_file_to_ftp(
    local_file,
    host,
    port,
    username,
    password,
    path,
    log,
):

    ftp = None

    try:

        append_process_log(
            log,
            f"Connecting securely to FTPS server: "
            f"{host}:{port}"
        )

        ftp = ftplib.FTP_TLS()

        ftp.connect(
            host=host,
            port=cint(port or 21),
            timeout=60,
        )
        ftp.auth()

        ftp.login(
            user=username,
            passwd=password,
        )
        ftp.prot_p()

        append_process_log(
            log,
            "FTPS authentication successful."
        )

        ftp_change_directory(
            ftp,
            path
        )

        append_process_log(
            log,
            f"Uploading securely: {local_file.name}"
        )

        with open(local_file, "rb") as file_handle:

            ftp.storbinary(
                f"STOR {local_file.name}",
                file_handle,
                blocksize=1024 * 1024,
            )

        append_process_log(
            log,
            f"Secure FTPS upload completed: "
            f"{local_file.name}"
        )

        return True

    except Exception as exc:

        append_process_log(
            log,
            f"FTPS upload failed for "
            f"{local_file.name}: {exc}"
        )

        frappe.log_error(
            frappe.get_traceback(),
            f"Backup Utility - FTPS Upload Failed - "
            f"{local_file.name}"
        )

        return False

    finally:

        if ftp:

            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass


def upload_backups_to_ftp(
    doc,
    backup_files,
    log,
):

    if not backup_files:

        append_process_log(
            log,
            "No new backup files found for FTP upload."
        )

        return True

    host = doc.host
    port = doc.port or 21
    username = doc.username
    password = doc.get_password("password")
    path = doc.path or "/"

    missing = []

    if not host:
        missing.append(_("FTP Host"))

    if not username:
        missing.append(_("FTP Username"))

    if not password:
        missing.append(_("FTP Password"))

    if missing:

        # These files were created but can't be uploaded due to missing
        # config - keep them protected from size-based cleanup.
        for backup_file in backup_files:
            mark_upload_pending(backup_file)

        raise frappe.ValidationError(
            _("{0} required for FTP upload.").format(", ".join(missing))
        )

    append_process_log(
        log,
        f"Starting FTP upload for "
        f"{len(backup_files)} file(s)."
    )

    all_success = True

    for backup_file in backup_files:

        success = upload_file_to_ftp(
            local_file=backup_file,
            host=host,
            port=port,
            username=username,
            password=password,
            path=path,
            log=log,
        )

        if success:

            clear_upload_marker(backup_file)

            if cint(doc.delete_local):

                try:

                    backup_file.unlink()

                    append_process_log(
                        log,
                        f"Deleted local backup after "
                        f"successful upload: "
                        f"{backup_file.name}"
                    )

                except Exception as exc:

                    all_success = False

                    append_process_log(
                        log,
                        f"Could not delete local backup "
                        f"{backup_file.name}: {exc}"
                    )

        else:

            all_success = False
            mark_upload_pending(backup_file)

            append_process_log(
                log,
                f"Local backup retained because "
                f"upload failed: {backup_file.name}"
            )

    return all_success


# Main Backup

def run_backup():

    doc = get_backup_utility()

    if not cint(doc.enabled):
        return

    backup_directory = get_backup_directory()

    if not acquire_backup_lock(backup_directory):
        logger.info(
            f"Backup Utility - Skipped for site {frappe.local.site}: "
            f"a backup is already in progress."
        )
        frappe.throw(
            _("A backup is already in progress. Please wait for it to finish.")
        )

    try:

        # Create exactly ONE Backup Log

        log = create_backup_log(doc)

        doc.db_set(
            "backup_log",
            log.name
        )

        append_process_log(
            log,
            "Backup process started."
        )

        # Capture files existing BEFORE this backup
        before_files = get_backup_files(
            backup_directory
        )

        # Build backup command

        command = [
            "bench",
            "--site",
            frappe.local.site,
            "backup",
        ]

        if cint(doc.include_files):
            command.append(
                "--with-files"
            )

        command.extend([
            "--backup-path",
            backup_directory,
        ])

        append_process_log(
            log,
            f"Executing backup command: "
            f"{' '.join(command)}"
        )

        # Execute backup

        try:

            result = subprocess.run(
                command,
                cwd=frappe.utils.get_bench_path(),
                capture_output=True,
                text=True,
                timeout=3600,
            )

        except Exception as exc:

            log.backup_status = "Failed"
            log.backup_error = str(exc)
            log.backup_at = now_datetime()

            append_process_log(
                log,
                f"Backup process failed: {exc}"
            )

            log.save(ignore_permissions=True)

            frappe.db.set_single_value(
                "Backup Utility", "last_backup_status", "Failed"
            )
            frappe.db.set_single_value(
                "Backup Utility", "last_backup_error", str(exc)
            )
            frappe.db.set_single_value(
                "Backup Utility", "last_backup_at", now_datetime()
            )

            frappe.log_error(
                frappe.get_traceback(),
                "Backup Utility - Backup Failed"
            )

            frappe.db.commit()
            raise

        # Backup command failed (non-zero exit)

        if result.returncode != 0:

            error_message = (
                result.stderr.strip()
                or result.stdout.strip()
                or "Backup command failed."
            )

            log.backup_status = "Failed"
            log.backup_error = error_message
            log.backup_at = now_datetime()
            log.save(ignore_permissions=True)

            frappe.db.set_single_value(
                "Backup Utility",
                "last_backup_at",
                now_datetime()
            )

            frappe.db.set_single_value(
                "Backup Utility",
                "last_backup_status",
                "Failed"
            )

            frappe.db.set_single_value(
                "Backup Utility",
                "last_backup_error",
                error_message
            )

            append_process_log(
                log,
                f"Backup failed: {error_message}"
            )

            frappe.db.commit()

            return

        # Find ONLY files created by this backup, and record them.
        # Anything that goes wrong here still leaves the log in its
        # default "Failed" state - it must NOT be able to overwrite a
        # "Success" that hasn't been recorded yet.

        try:

            after_files = get_backup_files(
                backup_directory
            )

            backup_files = sorted(
                after_files - before_files,
                key=lambda path: path.stat().st_mtime
            )

            append_process_log(
                log,
                f"Backup created successfully."
            )

            append_process_log(
                log,
                f"New backup files created: "
                f"{len(backup_files)}"
            )

            for backup_file in backup_files:

                size_mb = (
                    backup_file.stat().st_size
                    / (1024 * 1024)
                )

                append_process_log(
                    log,
                    f"Created: {backup_file.name} "
                    f"({size_mb:.2f} MB)"
                )

        except Exception as exc:

            log.backup_error = str(exc)
            log.backup_at = now_datetime()

            append_process_log(
                log,
                f"Backup process failed while collecting backup files: {exc}"
            )

            log.save(ignore_permissions=True)

            frappe.db.set_single_value(
                "Backup Utility", "last_backup_status", "Failed"
            )
            frappe.db.set_single_value(
                "Backup Utility", "last_backup_error", str(exc)
            )
            frappe.db.set_single_value(
                "Backup Utility", "last_backup_at", now_datetime()
            )

            frappe.log_error(
                frappe.get_traceback(),
                "Backup Utility - Backup Failed"
            )

            frappe.db.commit()
            raise

        # The backup itself succeeded - persist this NOW. Everything
        # below (upload, cleanup) is isolated so a failure there can
        # never flip this back to "Failed".

        backup_now = now_datetime()

        log.backup_status = "Success"
        log.backup_error = ""
        log.backup_at = backup_now

        frappe.db.set_single_value(
            "Backup Utility",
            "last_backup_at",
            backup_now
        )

        frappe.db.set_single_value(
            "Backup Utility",
            "last_backup_status",
            "Success"
        )

        frappe.db.set_single_value(
            "Backup Utility",
            "last_backup_error",
            ""
        )

        log.save(ignore_permissions=True)
        frappe.db.commit()

        # FTP - isolated from backup_status.

        if cint(doc.upload):

            log.upload_status = "Pending"

            append_process_log(
                log,
                "FTP upload enabled."
            )

            try:
                upload_success = upload_backups_to_ftp(
                    doc,
                    backup_files,
                    log,
                )
            except Exception as exc:
                upload_success = False
                append_process_log(
                    log,
                    f"FTP upload failed: {exc}"
                )
                frappe.log_error(
                    frappe.get_traceback(),
                    "Backup Utility - FTPS Upload Failed"
                )

            if upload_success:

                upload_now = now_datetime()

                log.upload_status = "Success"
                log.upload_at = upload_now
                log.upload_error = ""

                frappe.db.set_single_value(
                    "Backup Utility",
                    "last_upload_at",
                    upload_now
                )

                frappe.db.set_single_value(
                    "Backup Utility",
                    "last_upload_status",
                    "Success"
                )

                frappe.db.set_single_value(
                    "Backup Utility",
                    "last_upload_error",
                    ""
                )

                append_process_log(
                    log,
                    "FTP upload completed successfully."
                )

            else:

                log.upload_status = "Failed"
                log.upload_at = now_datetime()
                log.upload_error = (
                    "One or more backup files "
                    "failed to upload."
                )

                frappe.db.set_single_value(
                    "Backup Utility",
                    "last_upload_at",
                    now_datetime()
                )

                frappe.db.set_single_value(
                    "Backup Utility",
                    "last_upload_status",
                    "Failed"
                )

                frappe.db.set_single_value(
                    "Backup Utility",
                    "last_upload_error",
                    log.upload_error
                )

                append_process_log(
                    log,
                    "FTP upload completed with failures."
                )

        else:

            append_process_log(
                log,
                "FTP upload disabled."
            )

        # Local backup size cleanup - isolated from backup_status.

        try:
            cleanup_old_backups(
                backup_directory,
                doc.maximum_backup_size_mb,
                log,
            )
        except Exception:
            append_process_log(
                log,
                "Local backup cleanup failed unexpectedly."
            )
            frappe.log_error(
                frappe.get_traceback(),
                "Backup Utility - Cleanup Failed"
            )

        # Finalize

        append_process_log(
            log,
            "Backup process completed."
        )

        log.save(
            ignore_permissions=True
        )

        frappe.db.commit()

    finally:
        release_backup_lock(backup_directory)


# Manual Trigger

@frappe.whitelist()
def execute_backup():

    frappe.only_for("System Manager")

    doc = get_backup_utility()

    if not cint(doc.enabled):
        frappe.throw(
            _("Backup Utility is disabled. Enable it before running a backup.")
        )

    logger.info(
        f"Backup Utility - Execute started for site {frappe.local.site} "
        f"by user {frappe.session.user}."
    )

    try:

        run_backup()

        logger.info(
            f"Backup Utility - Execute finished for site {frappe.local.site}."
        )

        return {
            "success": True
        }

    except Exception:

        frappe.log_error(
            title="Backup Utility - Execute Failed",
            message=frappe.get_traceback(),
        )

        raise


def append_process_log(log, message):

    timestamp = now_datetime().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    new_line = f"[{timestamp}] {message}"

    if log.process_log:
        log.process_log += "\n" + new_line
    else:
        log.process_log = new_line

    log.save(ignore_permissions=True)


def create_backup_log(doc):

    log = frappe.new_doc("Backup Log")

    log.backup_utility = doc.name
    log.backup_at = now_datetime()

    if cint(doc.upload):

        if cint(doc.delete_local):
            log.process_type = (
                "Local, Upload and Delete"
            )
        else:
            log.process_type = (
                "Local and Upload"
            )

    else:

        log.process_type = "Local"

    log.backup_status = "Failed"
    log.upload_status = None
    log.delete_local = cint(doc.delete_local)

    log.insert(ignore_permissions=True)

    frappe.db.commit()

    return log
