frappe.ui.form.on("Backup Utility", {
    refresh(frm) {
        frm.trigger("setup_test_connection");
        frm.trigger("setup_save_state");
        update_connection_message(frm);
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