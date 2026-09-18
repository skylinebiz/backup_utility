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

    scheduler_event = get_scheduler_event()

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

        if job.scheduler_event != scheduler_event:
            job.scheduler_event = scheduler_event
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
        job.scheduler_event = scheduler_event

        job.insert(ignore_permissions=True)

    frappe.db.commit()


def get_scheduler_event():
    # `bench migrate` (sync_jobs -> clear_events) deletes every Scheduled Job
    # Type whose method isn't declared in hooks.scheduler_events, unless it
    # is linked to a Scheduler Event (or a Server Script). This schedule is
    # user-configured at runtime so it can't be declared in hooks.py - the
    # Scheduler Event link is what makes it persist across migrations.
    event = frappe.db.exists(
        "Scheduler Event",
        {
            "scheduled_against": "Backup Utility",
            "method": BACKUP_SCHEDULE_METHOD,
        },
    )

    if event:
        return event

    return frappe.get_doc(
        {
            "doctype": "Scheduler Event",
            "scheduled_against": "Backup Utility",
            "method": BACKUP_SCHEDULE_METHOD,
        }
    ).insert(ignore_permissions=True).name


def get_backup_cron(doc):

    if not doc.when:
        return None

    time_value = str(doc.when)

    hour, minute, second = map(
        int,
        time_value[:8].split(":"),
    )

    return f"{minute} {hour} * * *"