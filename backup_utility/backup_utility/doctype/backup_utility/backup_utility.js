frappe.ui.form.on("Backup Utility", {
    refresh(frm) {
        frm.trigger("setup_test_connection");
        frm.trigger("setup_save_state");
        frm.trigger("setup_start_backup_now");
        update_connection_message(frm);
    },

    setup_start_backup_now(frm) {

        // Placed in the page toolbar (next to Save) rather than as a
        // form field, so re-add it fresh on every refresh.
        frm.page.remove_inner_button(__("Start Backup Now"));

        const button = frm.add_custom_button(
            __("Start Backup Now"),
            function () {

                button.prop("disabled", true).text(__("Backup in Progress..."));

                frappe.call({
                    method: "backup_utility.api.backup.execute_backup",

                    freeze: true,
                    freeze_message: __("Running backup. This may take a while..."),

                    callback: function (r) {

                        if (r.message && r.message.success) {
                            frappe.show_alert({
                                message: __("Backup completed. See Backup Log for details."),
                                indicator: "green"
                            });
                        }
                    },

                    always: function () {
                        // Re-fetches the doc (updated status/log fields)
                        // and re-runs refresh, which resets the button.
                        frm.reload_doc();
                    }
                });
            }
        );

        button
            .removeClass("btn-default")
            .addClass("btn-primary")
            .attr(
                "title",
                __("Manually run a backup right now, independent of the scheduled time.")
            );

        frappe.call({
            method: "backup_utility.api.backup.get_backup_status",
            callback: function (r) {
                const running = !!(r.message && r.message.running);

                button.prop("disabled", running);
                button.text(
                    running ? __("Backup in Progress...") : __("Start Backup Now")
                );
            }
        });
    },

    setup_test_connection(frm) {
        const button = frm.fields_dict.test_connection;

        if (!button) {
            return;
        }

        button.$input.off("click");

        button.$input.on("click", function () {

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

                return (
                    value === undefined ||
                    value === null ||
                    String(value).trim() === ""
                );
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

                frm.scroll_to_field(
                    missing_fields[0].fieldname
                );

                return;
            }

            frappe.call({
                method: "backup_utility.api.backup.test_connection",

                // The doc may still be unsaved at this point (saving is
                // blocked until the connection is tested), so send what's
                // currently in the form instead of testing stale DB values.
                args: {
                    host: frm.doc.host,
                    port: frm.doc.port,
                    username: frm.doc.username,
                    password: frm.doc.password,
                    path: frm.doc.path
                },

                freeze: true,
                freeze_message: __("Testing FTP connection..."),

                callback: function (r) {

                    if (r.message && r.message.success) {

                        frm.set_value(
                            "connection_tested",
                            1
                        );

                        frm.enable_save();
                        update_connection_message(frm);

                        frappe.show_alert({
                            message: r.message.message,
                            indicator: "green"
                        });
                    }
                }
            });
        });
    },

    setup_save_state(frm) {
        if (!frm.doc.upload) {
            frm.enable_save();
            return;
        }

        if (frm.doc.connection_tested) {
            frm.enable_save();
        } else {
            frm.disable_save();
        }
    },

    upload(frm) {

        if (frm.doc.upload) {
            frm.set_value("connection_tested", 0);
            frm.disable_save();
        } else {
            frm.set_value("connection_tested", 0);
            frm.enable_save();
        }
        update_connection_message(frm);
    },

    host(frm) {
        frm.trigger("ftp_config_changed");
    },

    port(frm) {
        frm.trigger("ftp_config_changed");
    },

    username(frm) {
        frm.trigger("ftp_config_changed");
    },

    password(frm) {
        frm.trigger("ftp_config_changed");
    },

    path(frm) {
        frm.trigger("ftp_config_changed");
    },

    ftp_config_changed(frm) {
        if (!frm.doc.upload) {
            return;
        }

        // Configuration changed, previous test is no longer valid
        if (frm.doc.connection_tested) {
            frm.set_value(
                "connection_tested",
                0
            );
        }

        frm.disable_save();
        update_connection_message(frm);
    }
});


function update_connection_message(frm) {
    frm.dashboard.clear_headline();
    if (!frm.doc.upload) {
        return;
    }

    if (frm.doc.connection_tested) {
        return;
    }

    frm.dashboard.set_headline_alert(
        __("FTP connection needs to be tested before saving."),
        "orange"
    );
}