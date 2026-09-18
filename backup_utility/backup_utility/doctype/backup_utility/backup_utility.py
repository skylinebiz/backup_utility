import frappe

from frappe.model.document import Document
from frappe.utils import cint
from frappe import _


BACKUP_SCHEDULE_METHOD = (
    "backup_utility.api.backup.ftp_backup_cron"
)

class BackupUtility(Document):

    def validate(self):
        if self.enabled and not self.when:
            frappe.throw("Please enable and configure the backup time.")

        if self.upload and not self.connection_tested:
            frappe.throw(
                _(
                    "Please test the FTP connection successfully "
                    "before saving the Backup Utility."
                ))

    def on_update(self):
        update_backup_schedule(self)


def restore_backup_schedule():
    # `bench migrate` deletes any "Scheduled Job Type" whose method isn't
    # declared in an app's hooks.scheduler_events - which is every method
    # here, since this schedule is managed dynamically rather than via
    # hooks.py (see the note at the bottom of hooks.py). Re-create it from
    # the saved settings right after migrate so the schedule survives.
    update_backup_schedule(
        frappe.get_single("Backup Utility")
    )


def update_backup_schedule(doc):

    # The job record always exists (even when disabled / never
    # configured) so a manual trigger - which runs through this same
    # Scheduled Job Type, see backup_utility.api.backup.trigger_manual_backup -
    # is available from the very first save onward. "stopped" is what
    # actually gates the *automatic* daily firing.
    should_run = bool(doc.enabled and doc.when)

    # A placeholder cron is needed while stopped, since Scheduled Job
    # Type requires a valid cron_format for frequency "Cron" regardless
    # of "stopped" - it is never evaluated while stopped=1.
    cron = get_backup_cron(doc) or "0 0 * * *"

    job_name = frappe.db.exists(
        "Scheduled Job Type",
        {
            "method": BACKUP_SCHEDULE_METHOD,
        },
    )

    if job_name:

        job = frappe.get_doc(
            "Scheduled Job Type",
            job_name,
        )

        changed = False

        if job.frequency != "Cron":
            job.frequency = "Cron"
            changed = True

        if job.cron_format != cron:
            job.cron_format = cron
            changed = True

        if bool(job.stopped) != (not should_run):
            job.stopped = 0 if should_run else 1
            changed = True

        if not job.create_log:
            job.create_log = 1
            changed = True

        if changed:
            job.save(ignore_permissions=True)

    else:

        job = frappe.new_doc("Scheduled Job Type")

        job.method = BACKUP_SCHEDULE_METHOD
        job.frequency = "Cron"
        job.cron_format = cron
        job.stopped = 0 if should_run else 1
        job.create_log = 1

        job.insert(ignore_permissions=True)

    frappe.db.commit()


def get_backup_cron(doc):

    if not doc.when:
        return None

    time_value = str(doc.when)

    hour, minute, second = map(
        int,
        time_value[:8].split(":"),
    )

    return f"{minute} {hour} * * *"