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

                button.prop("disabled", true).text(__("Queuing..."));

                frappe.call({
                    method: "backup_utility.api.backup.trigger_manual_backup",

                    freeze: true,
                    freeze_message: __("Queuing backup via the scheduler..."),

                    callback: function (r) {

                        if (r.message && r.message.queued) {
                            frappe.show_alert({
                                message: __(
                                    "Backup queued via the scheduler. Check Backup Log for progress."
                                ),
                                indicator: "green"
                            });
                        }
                    },

                    always: function () {
                        // Re-checks status - it may still show as not
                        // running yet if the queue hasn't picked it up,
                        // which is expected for a moment after queuing.
                        frm.trigger("setup_start_backup_now");
                    }
                });
            }
        );

        button
            .removeClass("btn-default")
            .addClass("btn-primary")
            .attr(
                "title",
                __(
                    "Runs the backup schedule right now, through the same " +
                    "mechanism Frappe's scheduler uses - independent of the " +
                    "configured time and of whether Enabled is checked."
                )
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

            const required_fields = is_s3(frm) ? [
                {
                    fieldname: "s3_bucket",
                    label: __("Bucket")
                },
                {
                    fieldname: "s3_access_key_id",
                    label: __("Access Key ID")
                },
                {
                    fieldname: "s3_secret_access_key",
                    label: __("Secret Access Key")
                }
            ] : [
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
                    title: __("Missing Upload Settings"),
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
                // currently in the form instead of testing stale DB values -
                // including which backend is selected, otherwise switching
                // Configuration Type without saving first would test
                // whichever one was last saved.
                args: {
                    upload_type: frm.doc.upload_type,
                    host: frm.doc.host,
                    port: frm.doc.port,
                    username: frm.doc.username,
                    password: frm.doc.password,
                    path: frm.doc.path,
                    s3_bucket: frm.doc.s3_bucket,
                    s3_endpoint_url: frm.doc.s3_endpoint_url,
                    s3_region: frm.doc.s3_region,
                    s3_access_key_id: frm.doc.s3_access_key_id,
                    s3_secret_access_key: frm.doc.s3_secret_access_key,
                    s3_path: frm.doc.s3_path
                },

                freeze: true,
                freeze_message: __("Testing connection..."),

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

    upload_type(frm) {
        frm.trigger("upload_config_changed");
    },

    host(frm) {
        frm.trigger("upload_config_changed");
    },

    port(frm) {
        frm.trigger("upload_config_changed");
    },

    username(frm) {
        frm.trigger("upload_config_changed");
    },

    password(frm) {
        frm.trigger("upload_config_changed");
    },

    path(frm) {
        frm.trigger("upload_config_changed");
    },

    s3_bucket(frm) {
        frm.trigger("upload_config_changed");
    },

    s3_endpoint_url(frm) {
        frm.trigger("upload_config_changed");
    },

    s3_region(frm) {
        frm.trigger("upload_config_changed");
    },

    s3_access_key_id(frm) {
        frm.trigger("upload_config_changed");
    },

    s3_secret_access_key(frm) {
        frm.trigger("upload_config_changed");
    },

    s3_path(frm) {
        frm.trigger("upload_config_changed");
    },

    upload_config_changed(frm) {
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


function is_s3(frm) {
    return frm.doc.upload_type === "S3-compatible Object Storage";
}

function update_connection_message(frm) {
    frm.dashboard.clear_headline();
    if (!frm.doc.upload) {
        return;
    }

    if (frm.doc.connection_tested) {
        return;
    }

    frm.dashboard.set_headline_alert(
        __("Connection needs to be tested before saving."),
        "orange"
    );
}