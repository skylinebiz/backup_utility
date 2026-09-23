
import frappe
import ftplib
import hashlib
import hmac
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
import io
import urllib.error
import urllib.parse
import urllib.request
from frappe import _
from frappe.utils import cint, now_datetime, get_time, flt
import logging

logger = frappe.logger("backup_utility")
logger.setLevel(logging.INFO)


UPLOAD_TYPE_FTPS = "FTPS"
UPLOAD_TYPE_S3 = "S3-compatible Object Storage"


def get_upload_type(doc):
    # Older records saved before "Configuration Type" existed have no
    # value here - treat them as FTPS to preserve prior behaviour.
    return doc.upload_type or UPLOAD_TYPE_FTPS


def ftp_backup_cron():

    doc = get_backup_utility()

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


def is_backup_lock_active(backup_directory):

    lock_path = get_backup_lock_path(backup_directory)

    if not lock_path.exists():
        return False

    age = time.time() - lock_path.stat().st_mtime

    return age <= BACKUP_LOCK_STALE_SECONDS


def acquire_backup_lock(backup_directory):

    lock_path = get_backup_lock_path(backup_directory)

    if lock_path.exists():

        if is_backup_lock_active(backup_directory):
            return False

        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass

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


@frappe.whitelist()
def get_backup_status():

    frappe.only_for("System Manager")

    return {
        "running": is_backup_lock_active(
            get_backup_directory()
        )
    }


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


# S3-compatible Object Storage (AWS S3, Cloudflare R2, Backblaze B2, ...)
#
# Implemented with the standard library only (urllib + hashlib + hmac -
# a minimal AWS Signature Version 4 REST client) rather than a
# third-party SDK, to avoid adding a dependency for a single upload
# backend. Only the handful of operations Backup Utility needs are
# implemented (PutObject, HeadBucket, HeadObject, DeleteObject) - this
# is not a general-purpose S3 client.

def get_s3_endpoint_host(endpoint_url):
    # Easy mistake to make: pasting an example URL that already has the
    # bucket appended (some providers' dashboards show ".../<bucket>").
    # Path-style addressing then appends the bucket AGAIN, producing
    # ".../<bucket>/<bucket>" and a 404. An S3 endpoint is just a host -
    # strip any path/query/fragment.
    endpoint_url = (endpoint_url or "").strip()

    if not endpoint_url:
        return None

    parts = urllib.parse.urlsplit(endpoint_url)

    if not parts.scheme:
        # Allow "account.r2.cloudflarestorage.com" without a scheme.
        parts = urllib.parse.urlsplit(f"https://{endpoint_url}")

    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def get_s3_object_key(path, filename):

    prefix = (path or "").strip().strip("/")

    return f"{prefix}/{filename}" if prefix else filename


class S3Error(Exception):

    def __init__(self, status, reason, body=""):

        self.status = status
        self.reason = reason
        self.body = body

        message = f"{status or 'connection error'} {reason}".strip()

        if body:
            message = f"{message}: {body}"

        super().__init__(message)


def _s3_sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def _s3_hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _s3_signing_key(secret_key, date_stamp, region):
    k_date = _s3_hmac(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _s3_hmac(k_date, region)
    k_service = _s3_hmac(k_region, "s3")
    return _s3_hmac(k_service, "aws4_request")


class S3Client:
    """Minimal AWS Signature V4 S3 REST client (path-style addressing
    only - see get_s3_endpoint_host)."""

    def __init__(self, access_key_id, secret_access_key, endpoint_url=None, region=None):

        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.region = (region or "us-east-1").strip() or "us-east-1"

        host = get_s3_endpoint_host(endpoint_url)
        self.host = host or f"https://s3.{self.region}.amazonaws.com"
        self.host_header = urllib.parse.urlsplit(self.host).netloc

    def _request(self, method, path, headers=None, body=b""):

        headers = {key.lower(): value for key, value in (headers or {}).items()}

        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        payload_hash = (
            "UNSIGNED-PAYLOAD"
            if hasattr(body, "read")
            else _s3_sha256_hex(body)
        )

        canonical_uri = urllib.parse.quote(path, safe="/-_.~")

        headers["host"] = self.host_header
        headers["x-amz-date"] = amz_date
        headers["x-amz-content-sha256"] = payload_hash

        canonical_headers = "".join(
            f"{key}:{str(value).strip()}\n"
            for key, value in sorted(headers.items())
        )
        signed_headers = ";".join(sorted(headers))

        canonical_request = "\n".join([
            method,
            canonical_uri,
            "",  # no query string used by any operation here
            canonical_headers,
            signed_headers,
            payload_hash,
        ])

        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            _s3_sha256_hex(canonical_request.encode("utf-8")),
        ])

        signing_key = _s3_signing_key(self.secret_access_key, date_stamp, self.region)
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        request_body = body if (hasattr(body, "read") or body) else None

        request = urllib.request.Request(
            self.host + canonical_uri,
            data=request_body,
            method=method,
            headers=headers,
        )

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            raise S3Error(
                exc.code, exc.reason, exc.read().decode("utf-8", "replace")
            ) from exc
        except urllib.error.URLError as exc:
            raise S3Error(None, str(exc.reason)) from exc

    def head_bucket(self, bucket):
        self._request("HEAD", f"/{bucket}")

    def head_object(self, bucket, key):
        self._request("HEAD", f"/{bucket}/{key}")

    def delete_object(self, bucket, key):
        self._request("DELETE", f"/{bucket}/{key}")

    def put_bytes(self, bucket, key, data, content_type="text/plain; charset=utf-8"):

        headers = {
            "content-length": str(len(data)),
            "content-type": content_type,
        }

        self._request("PUT", f"/{bucket}/{key}", headers=headers, body=data)

    def put_file(self, bucket, key, file_path):

        headers = {
            "content-length": str(os.path.getsize(file_path)),
            "content-type": "application/octet-stream",
        }

        with open(file_path, "rb") as file_handle:
            self._request(
                "PUT",
                f"/{bucket}/{key}",
                headers=headers,
                body=file_handle,
            )


def test_s3_connection(
    doc, bucket=None, endpoint_url=None, region=None,
    access_key_id=None, secret_access_key=None, path=None,
):

    # Same reasoning as test_ftp_connection: prefer the live form values
    # over the saved doc, since this is often called before the document
    # can even be saved.
    bucket = bucket if bucket not in (None, "") else doc.s3_bucket
    endpoint_url = endpoint_url if endpoint_url not in (None, "") else doc.s3_endpoint_url
    region = region if region not in (None, "") else doc.s3_region
    access_key_id = access_key_id if access_key_id not in (None, "") else doc.s3_access_key_id
    path = path if path not in (None, "") else (doc.s3_path or "")

    if not secret_access_key or is_masked_password(secret_access_key):
        secret_access_key = doc.get_password("s3_secret_access_key")

    missing = []

    if not bucket:
        missing.append(_("Bucket"))

    if not access_key_id:
        missing.append(_("Access Key ID"))

    if not secret_access_key:
        missing.append(_("Secret Access Key"))

    if missing:
        frappe.throw(
            _("{0} required.").format(", ".join(missing))
        )

    client = S3Client(
        access_key_id,
        secret_access_key,
        endpoint_url=endpoint_url,
        region=region,
    )

    test_filename = (
        f"backup_utility_connection_test_"
        f"{now_datetime().strftime('%Y%m%d%H%M%S%f')}.txt"
    )

    key = get_s3_object_key(path, test_filename)

    test_content = (
        "Backup Utility S3 connection test.\n"
        "This file will be deleted automatically."
    ).encode("utf-8")

    try:

        logger.info(
            f"Backup Utility - Testing S3-compatible connection "
            f"to bucket {bucket}."
        )

        client.head_bucket(bucket)

        client.put_bytes(bucket, key, test_content)

        logger.info(
            f"Backup Utility - S3 test file uploaded: {key}"
        )

        client.head_object(bucket, key)

        client.delete_object(bucket, key)

        logger.info(
            f"Backup Utility - S3 test file deleted: {key}"
        )

        return {
            "success": True,
            "message": _(
                "S3 connection successful. "
                "Authentication, bucket access and write permission verified."
            ),
        }

    except Exception as exc:
        logger.exception(
            "Backup Utility - S3 connection test failed."
        )
        frappe.log_error(
            frappe.get_traceback(),
            "Backup Utility - S3 Connection Test Failed",
        )
        frappe.throw(
            _(
                "S3 connection test failed: {0}"
            ).format(str(exc))
        )


def upload_backups_to_s3(
    doc,
    backup_files,
    log,
):

    if not backup_files:

        append_process_log(
            log,
            "No new backup files found for S3 upload."
        )

        return True

    bucket = doc.s3_bucket
    access_key_id = doc.s3_access_key_id
    secret_access_key = doc.get_password("s3_secret_access_key")
    path = doc.s3_path or ""

    missing = []

    if not bucket:
        missing.append(_("Bucket"))

    if not access_key_id:
        missing.append(_("Access Key ID"))

    if not secret_access_key:
        missing.append(_("Secret Access Key"))

    if missing:

        # These files were created but can't be uploaded due to missing
        # config - keep them protected from size-based cleanup.
        for backup_file in backup_files:
            mark_upload_pending(backup_file)

        raise frappe.ValidationError(
            _("{0} required for S3 upload.").format(", ".join(missing))
        )

    append_process_log(
        log,
        f"Starting S3 upload for "
        f"{len(backup_files)} file(s)."
    )

    client = S3Client(
        access_key_id,
        secret_access_key,
        endpoint_url=doc.s3_endpoint_url,
        region=doc.s3_region,
    )

    all_success = True

    for backup_file in backup_files:

        key = get_s3_object_key(path, backup_file.name)

        try:

            append_process_log(
                log,
                f"Uploading to S3-compatible storage: "
                f"{backup_file.name} -> s3://{bucket}/{key}"
            )

            client.put_file(bucket, key, backup_file)

            append_process_log(
                log,
                f"S3 upload completed: {backup_file.name}"
            )

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

        except Exception as exc:

            all_success = False
            mark_upload_pending(backup_file)

            append_process_log(
                log,
                f"S3 upload failed for "
                f"{backup_file.name}: {exc}"
            )

            frappe.log_error(
                frappe.get_traceback(),
                f"Backup Utility - S3 Upload Failed - "
                f"{backup_file.name}"
            )

    return all_success


# Upload dispatch - routes to the configured backend (FTPS / S3).

def upload_backups(
    doc,
    backup_files,
    log,
):

    if get_upload_type(doc) == UPLOAD_TYPE_S3:
        return upload_backups_to_s3(doc, backup_files, log)

    return upload_backups_to_ftp(doc, backup_files, log)


# Main Backup

def run_backup():

    doc = get_backup_utility()

    # "Enabled" only gates the automatic daily schedule (ftp_backup_cron
    # checks it before enqueueing) - a manual trigger via execute_backup
    # should work regardless of whether scheduling is turned on.

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

        # Upload - isolated from backup_status.

        if cint(doc.upload):

            upload_type = get_upload_type(doc)

            log.upload_status = "Pending"

            append_process_log(
                log,
                f"Upload enabled ({upload_type})."
            )

            try:
                upload_success = upload_backups(
                    doc,
                    backup_files,
                    log,
                )
            except Exception as exc:
                upload_success = False
                append_process_log(
                    log,
                    f"Upload failed: {exc}"
                )
                frappe.log_error(
                    frappe.get_traceback(),
                    "Backup Utility - Upload Failed"
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
                    "Upload completed successfully."
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
                    "Upload completed with failures."
                )

        else:

            append_process_log(
                log,
                "Upload disabled."
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


@frappe.whitelist()
def trigger_manual_backup():
    """Manually run a backup through the exact same path Frappe's own
    scheduler uses (Scheduled Job Type.enqueue(force=True)), rather than
    calling execute_backup directly.

    This means it goes through frappe.core...run_scheduled_job just like
    an automatic firing would, and is deduplicated against an
    already-queued run at that level (frappe.utils.background_jobs).
    "stopped" (kept in sync with Enabled/When by update_backup_schedule)
    only gates the *automatic* tick, since enqueue_events() filters on
    it before calling .enqueue() - calling .enqueue(force=True) directly
    on the doc here bypasses that filter, so this works regardless of
    whether Enabled is checked.

    The actual "already running" protection remains the file lock inside
    run_backup(), acquired once ftp_backup_cron's own enqueue reaches the
    long queue - this only adds a faster, earlier reject for a near-
    simultaneous second click.
    """

    frappe.only_for("System Manager")

    from backup_utility.backup_utility.doctype.backup_utility.backup_utility import (
        BACKUP_SCHEDULE_METHOD,
    )

    job_name = frappe.db.exists(
        "Scheduled Job Type",
        {"method": BACKUP_SCHEDULE_METHOD},
    )

    if not job_name:
        frappe.throw(
            _(
                "No backup schedule found yet. Save the Backup Utility "
                "settings once to create it, then try again."
            )
        )

    job = frappe.get_doc("Scheduled Job Type", job_name)

    queued = job.enqueue(force=True)

    if not queued:
        frappe.throw(
            _(
                "A backup is already queued or running via the scheduler. "
                "Please wait for it to finish."
            )
        )

    logger.info(
        f"Backup Utility - Manual trigger queued via Scheduled Job Type "
        f"for site {frappe.local.site} by user {frappe.session.user}."
    )

    return {
        "queued": True
    }


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



# A Password field sends back a dummy value of asterisks - as many as
# the real password's length, NOT a fixed "*****" - when the user hasn't
# retyped it (see BaseDocument._save_passwords). That's not a real
# password.
def is_masked_password(value):
    return bool(value) and set(value) == {"*"}


@frappe.whitelist()
def test_connection(
    upload_type=None,
    host=None, port=None, username=None, password=None, path=None,
    s3_bucket=None, s3_endpoint_url=None, s3_region=None,
    s3_access_key_id=None, s3_secret_access_key=None, s3_path=None,
):

    frappe.only_for("System Manager")

    doc = get_backup_utility()

    # The form may hold unsaved edits (this is often called before the
    # document can even be saved - see BackupUtility.validate). Prefer
    # whatever the caller just typed and only fall back to the saved
    # value when a field was left untouched - including which backend
    # (Configuration Type) is currently selected, otherwise switching
    # types without saving first would test the previously saved one.
    upload_type = upload_type if upload_type not in (None, "") else get_upload_type(doc)

    if upload_type == UPLOAD_TYPE_S3:
        return test_s3_connection(
            doc,
            bucket=s3_bucket,
            endpoint_url=s3_endpoint_url,
            region=s3_region,
            access_key_id=s3_access_key_id,
            secret_access_key=s3_secret_access_key,
            path=s3_path,
        )

    return test_ftp_connection(
        doc,
        host=host,
        port=port,
        username=username,
        password=password,
        path=path,
    )


def test_ftp_connection(doc, host=None, port=None, username=None, password=None, path=None):

    host = host if host not in (None, "") else doc.host
    port = cint(port) if port not in (None, "") else cint(doc.port or 21)
    username = username if username not in (None, "") else doc.username
    path = path if path not in (None, "") else (doc.path or "/")

    if not password or is_masked_password(password):
        password = doc.get_password("password")

    # Validate configuration
    missing = []

    if not host:
        missing.append(_("FTP Host"))

    if not username:
        missing.append(_("FTP Username"))

    if not password:
        missing.append(_("FTP Password"))

    if not port:
        missing.append(_("FTP Port"))


    if missing:
        frappe.throw(
            _("{0} required.").format(", ".join(missing))
        )

    ftp = None
    test_filename = (
        f"backup_utility_connection_test_"
        f"{frappe.utils.now_datetime().strftime('%Y%m%d%H%M%S%f')}.txt"
    )

    try:

        # Connect
        logger.info(
            f"Backup Utility - Testing FTPS connection "
            f"to {host}:{port}."
        )

        ftp = ftplib.FTP_TLS()

        ftp.connect(
            host=host,
            port=port,
            timeout=60,
        )

        ftp.auth()

        # Login
        ftp.login(
            user=username,
            passwd=password,
        )

        # Encrypt data connection
        ftp.prot_p()

        # Check remote directory
        ftp_change_directory(
            ftp,
            path
        )

        # Test Write permission
        test_content = (
            "Backup Utility FTP connection test.\n"
            "This file will be deleted automatically."
        )

        test_file = io.BytesIO(
            test_content.encode("utf-8")
        )

        ftp.storbinary(
            f"STOR {test_filename}",
            test_file,
        )

        logger.info(
            f"Backup Utility - FTP test file uploaded: "
            f"{test_filename}"
        )

        # Verify file exists
        try:
            ftp.size(test_filename)
        except Exception:
            filenames = ftp.nlst()

            if test_filename not in filenames:
                raise Exception(
                    "Test file was uploaded but could not be verified."
                )

        # Delete test file
        ftp.delete(test_filename)

        logger.info(
            f"Backup Utility - FTP test file deleted: "
            f"{test_filename}"
        )

        return {
            "success": True,
            "message": _(
                "FTPS connection successful. "
                "Authentication, write permission verified."
            ),
        }

    except Exception as exc:
        logger.exception(
            "Backup Utility - FTPS connection test failed."
        )
        frappe.log_error(
            frappe.get_traceback(),
            "Backup Utility - FTPS Connection Test Failed",
        )
        frappe.throw(
            _(
                "FTPS connection test failed: {0}"
            ).format(str(exc))
        )

    finally:
        if ftp:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass