import frappe

from frappe.model.document import Document
from frappe.utils import cint


BACKUP_SCHEDULE_METHOD = (
    "backup_utility.api.backup.enqueue_scheduled_backup"
)


class BackupUtility(Document):

    def validate(self):
        if self.enabled and not self.when:
            frappe.throw("Please configure the backup time.")

    def on_save(self):
        update_backup_schedule(self)
        
    def on_update(self):
        update_backup_schedule(self)


def update_backup_schedule(doc):

    job_name = frappe.db.exists(
        "Scheduled Job Type",
        {
            "method": BACKUP_SCHEDULE_METHOD,
        },
    )

    # Backup disabled or no time configured
    if not doc.enabled or not doc.when:

        if job_name:
            frappe.delete_doc(
                "Scheduled Job Type",
                job_name,
                ignore_permissions=True,
            )

        return

    cron = get_backup_cron(doc)

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

        if job.stopped:
            job.stopped = 0
            changed = True

        if changed:
            job.save(ignore_permissions=True)

    else:

        job = frappe.new_doc("Scheduled Job Type")

        job.method = BACKUP_SCHEDULE_METHOD
        job.frequency = "Cron"
        job.cron_format = cron
        job.stopped = 0

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