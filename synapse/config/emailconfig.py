#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
# Copyright 2015-2016 OpenMarket Ltd
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

# This file can't be called email.py because if it is, we cannot:
import email.utils
import logging
import os
from typing import Any

import attr

from synapse.types import JsonDict

from ._base import Config, ConfigError

logger = logging.getLogger(__name__)

MISSING_PASSWORD_RESET_CONFIG_ERROR = """\
Password reset emails are enabled on this homeserver due to a partial
'email' block. However, the following required keys are missing:
    %s
"""

DEFAULT_SUBJECTS = {
    "message_from_person_in_room": "[%(app)s] You have a message on %(app)s from %(person)s in the %(room)s room...",
    "message_from_person": "[%(app)s] You have a message on %(app)s from %(person)s...",
    "messages_from_person": "[%(app)s] You have messages on %(app)s from %(person)s...",
    "messages_in_room": "[%(app)s] You have messages on %(app)s in the %(room)s room...",
    "messages_in_room_and_others": "[%(app)s] You have messages on %(app)s in the %(room)s room and others...",
    "messages_from_person_and_others": "[%(app)s] You have messages on %(app)s from %(person)s and others...",
    "invite_from_person": "[%(app)s] %(person)s has invited you to chat on %(app)s...",
    "invite_from_person_to_room": "[%(app)s] %(person)s has invited you to join the %(room)s room on %(app)s...",
    "invite_from_person_to_space": "[%(app)s] %(person)s has invited you to join the %(space)s space on %(app)s...",
    "password_reset": "[%(server_name)s] Password reset",
    "email_validation": "[%(server_name)s] Validate your email",
    "email_already_in_use": "[%(server_name)s] Email already in use",
}

LEGACY_TEMPLATE_DIR_WARNING = """
This server's configuration file is using the deprecated 'template_dir' setting in the
'email' section. Support for this setting has been deprecated and will be removed in a
future version of Synapse. Server admins should instead use the new
'custom_template_directory' setting documented here:
https://element-hq.github.io/synapse/latest/templates.html
---------------------------------------------------------------------------------------"""


@attr.s(slots=True, frozen=True, auto_attribs=True)
class EmailSubjectConfig:
    message_from_person_in_room: str
    message_from_person: str
    messages_from_person: str
    messages_in_room: str
    messages_in_room_and_others: str
    messages_from_person_and_others: str
    invite_from_person: str
    invite_from_person_to_room: str
    invite_from_person_to_space: str
    password_reset: str
    email_validation: str
    email_already_in_use: str


class EmailConfig(Config):
    section = "email"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        # TODO: We should separate better the email configuration from the notification
        # and account validity config.

        self.email_enable_notifs = False

        email_config = config.get("email")
        if email_config is None:
            email_config = {}

        self.force_tls = email_config.get("force_tls", False)
        self.email_smtp_host = email_config.get("smtp_host", "localhost")
        self.email_smtp_port = email_config.get(
            "smtp_port", 465 if self.force_tls else 25
        )
        self.email_smtp_user = email_config.get("smtp_user", None)
        self.email_smtp_pass = email_config.get("smtp_pass", None)
        self.require_transport_security = email_config.get(
            "require_transport_security", False
        )
        self.enable_smtp_tls = email_config.get("enable_tls", True)
        if self.force_tls and not self.enable_smtp_tls:
            raise ConfigError("email.force_tls requires email.enable_tls to be true")
        if self.require_transport_security and not self.enable_smtp_tls:
            raise ConfigError(
                "email.require_transport_security requires email.enable_tls to be true"
            )

        if "app_name" in email_config:
            self.email_app_name = email_config["app_name"]
        else:
            self.email_app_name = "Matrix"

        # TODO: Rename notif_from to something more generic, or have a separate
        # from for password resets, message notifications, etc?
        # Currently the email section is a bit bogged down with settings for
        # multiple functions. Would be good to split it out into separate
        # sections and only put the common ones under email:
        self.email_notif_from = email_config.get("notif_from", None)
        if self.email_notif_from is not None:
            # make sure it's valid
            parsed = email.utils.parseaddr(self.email_notif_from)
            if parsed[1] == "":
                raise RuntimeError("Invalid notif_from address")

        # A user-configurable template directory
        template_dir = email_config.get("template_dir")
        if template_dir is not None:
            logger.warning(LEGACY_TEMPLATE_DIR_WARNING)

        if isinstance(template_dir, str):
            # We need an absolute path, because we change directory after starting (and
            # we don't yet know what auxiliary templates like mail.css we will need).
            template_dir = os.path.abspath(template_dir)
        elif template_dir is not None:
            # If template_dir is something other than a str or None, warn the user
            raise ConfigError("Config option email.template_dir must be type str")

        self.email_enable_notifs = email_config.get("enable_notifs", False)

        if config.get("trust_identity_server_for_password_resets"):
            raise ConfigError(
                'The config option "trust_identity_server_for_password_resets" '
                "is no longer supported. Please remove it from the config file."
            )

        # If we have email config settings, assume that we can verify ownership of
        # email addresses.
        self.can_verify_email = email_config != {}

        # Get lifetime of a validation token in milliseconds
        self.email_validation_token_lifetime = self.parse_duration(
            email_config.get("validation_token_lifetime", "1h")
        )

        if self.can_verify_email:
            missing = []
            if not self.email_notif_from:
                missing.append("email.notif_from")

            if missing:
                raise ConfigError(
                    MISSING_PASSWORD_RESET_CONFIG_ERROR % (", ".join(missing),)
                )

            # These email templates have placeholders in them, and thus must be
            # parsed using a templating engine during a request
            password_reset_template_html = email_config.get(
                "password_reset_template_html", "password_reset.html"
            )
            password_reset_template_text = email_config.get(
                "password_reset_template_text", "password_reset.txt"
            )
            registration_template_html = email_config.get(
                "registration_template_html", "registration.html"
            )
            registration_template_text = email_config.get(
                "registration_template_text", "registration.txt"
            )
            already_in_use_template_html = email_config.get(
                "already_in_use_template_html", "already_in_use.html"
            )
            already_in_use_template_text = email_config.get(
                "already_in_use_template_html", "already_in_use.txt"
            )
            add_threepid_template_html = email_config.get(
                "add_threepid_template_html", "add_threepid.html"
            )
            add_threepid_template_text = email_config.get(
                "add_threepid_template_text", "add_threepid.txt"
            )

            password_reset_template_failure_html = email_config.get(
                "password_reset_template_failure_html", "password_reset_failure.html"
            )
            registration_template_failure_html = email_config.get(
                "registration_template_failure_html", "registration_failure.html"
            )
            add_threepid_template_failure_html = email_config.get(
                "add_threepid_template_failure_html", "add_threepid_failure.html"
            )

            # These templates do not support any placeholder variables, so we
            # will read them from disk once during setup
            password_reset_template_success_html = email_config.get(
                "password_reset_template_success_html", "password_reset_success.html"
            )
            registration_template_success_html = email_config.get(
                "registration_template_success_html", "registration_success.html"
            )
            add_threepid_template_success_html = email_config.get(
                "add_threepid_template_success_html", "add_threepid_success.html"
            )

            # Read all templates from disk
            (
                self.email_password_reset_template_html,
                self.email_password_reset_template_text,
                self.email_registration_template_html,
                self.email_registration_template_text,
                self.email_already_in_use_template_html,
                self.email_already_in_use_template_text,
                self.email_add_threepid_template_html,
                self.email_add_threepid_template_text,
                self.email_password_reset_template_confirmation_html,
                self.email_password_reset_template_failure_html,
                self.email_registration_template_failure_html,
                self.email_add_threepid_template_failure_html,
                password_reset_template_success_html_template,
                registration_template_success_html_template,
                add_threepid_template_success_html_template,
            ) = self.read_templates(
                [
                    password_reset_template_html,
                    password_reset_template_text,
                    registration_template_html,
                    registration_template_text,
                    already_in_use_template_html,
                    already_in_use_template_text,
                    add_threepid_template_html,
                    add_threepid_template_text,
                    "password_reset_confirmation.html",
                    password_reset_template_failure_html,
                    registration_template_failure_html,
                    add_threepid_template_failure_html,
                    password_reset_template_success_html,
                    registration_template_success_html,
                    add_threepid_template_success_html,
                ],
                (
                    td
                    for td in (
                        self.root.server.custom_template_directory,
                        template_dir,
                    )
                    if td
                ),  # Filter out template_dir if not provided
            )

            # Render templates that do not contain any placeholders
            self.email_password_reset_template_success_html_content = (
                password_reset_template_success_html_template.render()
            )
            self.email_registration_template_success_html_content = (
                registration_template_success_html_template.render()
            )
            self.email_add_threepid_template_success_html_content = (
                add_threepid_template_success_html_template.render()
            )

        if self.email_enable_notifs:
            missing = []
            if not self.email_notif_from:
                missing.append("email.notif_from")

            if missing:
                raise ConfigError(
                    "email.enable_notifs is True but required keys are missing: %s"
                    % (", ".join(missing),)
                )

            notif_template_html = email_config.get(
                "notif_template_html", "notif_mail.html"
            )
            notif_template_text = email_config.get(
                "notif_template_text", "notif_mail.txt"
            )

            (
                self.email_notif_template_html,
                self.email_notif_template_text,
            ) = self.read_templates(
                [notif_template_html, notif_template_text],
                (
                    td
                    for td in (
                        self.root.server.custom_template_directory,
                        template_dir,
                    )
                    if td
                ),  # Filter out template_dir if not provided
            )

            self.email_notif_for_new_users = email_config.get(
                "notif_for_new_users", True
            )
            self.email_riot_base_url = email_config.get(
                "client_base_url", email_config.get("riot_base_url", None)
            )
            # The amount of time we always wait before ever emailing about a notification
            # (to give the user a chance to respond to other push or notice the window)
            self.notif_delay_before_mail_ms = Config.parse_duration(
                email_config.get("notif_delay_before_mail", "10m")
            )

        if self.root.account_validity.account_validity_renew_by_email_enabled:
            expiry_template_html = email_config.get(
                "expiry_template_html", "notice_expiry.html"
            )
            expiry_template_text = email_config.get(
                "expiry_template_text", "notice_expiry.txt"
            )

            (
                self.account_validity_template_html,
                self.account_validity_template_text,
            ) = self.read_templates(
                [expiry_template_html, expiry_template_text],
                (
                    td
                    for td in (
                        self.root.server.custom_template_directory,
                        template_dir,
                    )
                    if td
                ),  # Filter out template_dir if not provided
            )

        subjects_config = email_config.get("subjects", {})
        subjects = {}

        for key, default in DEFAULT_SUBJECTS.items():
            subjects[key] = subjects_config.get(key, default)

        self.email_subjects = EmailSubjectConfig(**subjects)

        # The invite client location should be a HTTP(S) URL or None.
        self.invite_client_location = email_config.get("invite_client_location") or None
        if self.invite_client_location:
            if not isinstance(self.invite_client_location, str):
                raise ConfigError(
                    "Config option email.invite_client_location must be type str"
                )
            if not (
                self.invite_client_location.startswith("http://")
                or self.invite_client_location.startswith("https://")
            ):
                raise ConfigError(
                    "Config option email.invite_client_location must be a http or https URL",
                    path=("email", "invite_client_location"),
                )
