// Copyright (c) 2026, Harshit Jain and contributors
// For license information, please see license.txt

frappe.ui.form.on("Backup Utility", {
    refresh(frm) {
        frm.trigger("setup_test_connection");
    },

    setup_test_connection(frm) {
        frm.fields_dict.test_connection.$input.off("click");

        frm.fields_dict.test_connection.$input.on("click", function () {

            const required_fields = [
                {
                    fieldname: "host",
                    label: __("Host")
                },
                {
                    fieldname: "port",
                    label: __("Port")
                },
                {
                    fieldname: "username",
                    label: __("Username")
                },
                {
                    fieldname: "password",
                    label: __("Password")
                },
                {
                    fieldname: "path",
                    label: __("Path")
                }
            ];

            const missing_fields = required_fields.filter(field => {
                const value = frm.doc[field.fieldname];

                return value === undefined ||
                    value === null ||
                    String(value).trim() === "";
            });

            if (missing_fields.length) {

                const missing_names = missing_fields
                    .map(field => field.label)
                    .join(", ");

                frappe.msgprint({
                    title: __("Missing FTP Settings"),
                    message: __(
                        "Please enter the following fields before testing the connection:<br><br>{0}",
                        [missing_names]
                    ),
                    indicator: "red"
                });

                // Focus first missing field
                frm.scroll_to_field(
                    missing_fields[0].fieldname
                );

                return;
            }

            frappe.call({
                method: "backup_utility.api.backup.test_connection",

                freeze: true,
                freeze_message: __("Testing FTP connection..."),

                callback: function (r) {

                    if (r.message && r.message.success) {

                        frappe.show_alert({
                            message: r.message.message,
                            indicator: "green"
                        });

                    }
                }
            });
        });
    }
});