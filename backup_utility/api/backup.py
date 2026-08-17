
import frappe
import ftplib
import os
import subprocess
from pathlib import Path
from datetime import datetime

from frappe import _
from frappe.utils import cint, now_datetime, get_time, flt

def enqueue_scheduled_backup():

    doc = get_backup_utility()

    if not cint(doc.enabled):
        frappe.log_error(
            title="Backup Utility - Scheduler Skipped",
            message="Backup Utility is disabled.",
        )
        return

    if not doc.when:
        frappe.log_error(
            title="Backup Utility - Scheduler Skipped",
            message="Backup Utility has no configured time.",
        )
        return

    frappe.log_error(
        title="Backup Utility - Scheduler Triggered",
        message=(
            f"Site: {frappe.local.site}\n"
            f"Configured Time: {doc.when}\n"
            f"Queueing execute_backup on long queue."
        ),
    )

    job = frappe.enqueue(
        "backup_utility.api.backup.execute_backup",
        queue="long",
        timeout=3600,
        job_name=f"backup_utility_scheduled_backup:{frappe.local.site}",
        at_front=True,
    )

    frappe.log_error(
        title="Backup Utility - Backup Queued",
        message=(
            f"Site: {frappe.local.site}\n"
            f"Job ID: {job.id if job else 'UNKNOWN'}\n"
            f"Queue: long"
        ),
    )


def get_backup_utility():
    return frappe.get_single("Backup Utility")


def get_backup_directory():
    backup_directory = frappe.get_site_path("private", "backups")

    os.makedirs(backup_directory, exist_ok=True)

    return os.path.abspath(backup_directory)


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

    files = list(
        get_backup_files(backup_directory)
    )

    files.sort(
        key=lambda path: path.stat().st_mtime
    )

    total_size = sum(
        path.stat().st_size
        for path in files
        if path.exists()
    )

    append_process_log(
        log,
        f"Current local backup size: "
        f"{total_size / (1024 * 1024):.2f} MB. "
        f"Maximum allowed: {max_size_mb:.2f} MB."
    )

    while total_size > max_size_bytes and files:

        oldest = files.pop(0)

        try:

            file_size = oldest.stat().st_size
            oldest.unlink()
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
            f"Connecting to FTP server: "
            f"{host}:{port}"
        )

        ftp = ftplib.FTP()

        ftp.connect(
            host=host,
            port=cint(port or 21),
            timeout=60,
        )

        ftp.login(
            user=username,
            passwd=password,
        )

        append_process_log(
            log,
            "FTP authentication successful."
        )

        ftp_change_directory(
            ftp,
            path
        )

        append_process_log(
            log,
            f"Uploading: {local_file.name}"
        )

        with open(local_file, "rb") as file_handle:

            ftp.storbinary(
                f"STOR {local_file.name}",
                file_handle,
                blocksize=1024 * 1024,
            )

        append_process_log(
            log,
            f"Upload completed: {local_file.name}"
        )

        return True

    except Exception as exc:

        append_process_log(
            log,
            f"FTP upload failed for "
            f"{local_file.name}: {exc}"
        )

        frappe.log_error(
            frappe.get_traceback(),
            f"Backup Utility - FTP Upload Failed - "
            f"{local_file.name}"
        )

        return False

    finally:

        if ftp:

            try:
                ftp.quit()
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

    if not host:
        raise frappe.ValidationError(
            _("FTP Host is required.")
        )

    if not username:
        raise frappe.ValidationError(
            _("FTP Username is required.")
        )

    if not password:
        raise frappe.ValidationError(
            _("FTP Password is required.")
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

    try:

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

        result = subprocess.run(
            command,
            cwd=frappe.utils.get_bench_path(),
            capture_output=True,
            text=True,
            timeout=3600,
        )

        # Backup failed

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

            return

        # Find ONLY files created by this backup

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

        # Update Backup Utility

        backup_now = now_datetime()

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

        log.backup_status = "Success"
        log.backup_at = backup_now

        # FTP

        if cint(doc.upload):

            log.upload_status = "Pending"

            append_process_log(
                log,
                "FTP upload enabled."
            )

            upload_success = upload_backups_to_ftp(
                doc,
                backup_files,
                log,
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

        # Local backup size cleanup

        cleanup_old_backups(
            backup_directory,
            doc.maximum_backup_size_mb,
            log,
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

    except Exception as exc:

        log.backup_status = "Failed"
        log.backup_error = str(exc)

        append_process_log(
            log,
            f"Backup process failed: {exc}"
        )

        log.save(
            ignore_permissions=True
        )

        frappe.db.set_single_value(
            "Backup Utility",
            "last_backup_status",
            "Failed"
        )

        frappe.db.set_single_value(
            "Backup Utility",
            "last_backup_error",
            str(exc)
        )

        frappe.db.set_single_value(
            "Backup Utility",
            "last_backup_at",
            now_datetime()
        )

        frappe.log_error(
            frappe.get_traceback(),
            "Backup Utility - Backup Failed"
        )

        frappe.db.commit()

        raise


# Manual Trigger

@frappe.whitelist()
def execute_backup():

    frappe.log_error(
        title="Backup Utility - Execute Started",
        message=(
            f"Site: {frappe.local.site}\n"
            f"User: {frappe.session.user}"
        ),
    )

    try:

        result = run_backup()

        frappe.log_error(
            title="Backup Utility - Execute Finished",
            message=(
                f"Site: {frappe.local.site}\n"
                f"Result: {result}"
            ),
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

