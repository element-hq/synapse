#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org C.I.C. Foundation
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

import email.utils
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO
from typing import TYPE_CHECKING

from twisted.internet.defer import Deferred
from twisted.internet.endpoints import HostnameEndpoint
from twisted.internet.interfaces import IProtocolFactory
from twisted.internet.ssl import optionsForClientTLS
from twisted.mail.smtp import ESMTPSenderFactory
from twisted.protocols.tls import TLSMemoryBIOFactory

from synapse.logging.context import make_deferred_yieldable
from synapse.types import ISynapseReactor

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


async def _sendmail(
    reactor: ISynapseReactor,
    smtphost: str,
    smtpport: int,
    from_addr: str,
    to_addr: str,
    msg_bytes: bytes,
    username: bytes | None = None,
    password: bytes | None = None,
    require_auth: bool = False,
    require_tls: bool = False,
    enable_tls: bool = True,
    force_tls: bool = False,
    tlsname: str | None = None,
) -> None:
    """A simple wrapper around ESMTPSenderFactory, to allow substitution in tests

    Params:
        reactor: reactor to use to make the outbound connection
        smtphost: hostname to connect to
        smtpport: port to connect to
        from_addr: "From" address for email
        to_addr: "To" address for email
        msg_bytes: Message content
        username: username to authenticate with, if auth is enabled
        password: password to give when authenticating
        require_auth: if auth is not offered, fail the request
        require_tls: if TLS is not offered, fail the reqest
        enable_tls: True to enable STARTTLS. If this is False and require_tls is True,
           the request will fail.
        force_tls: True to enable Implicit TLS.
        tlsname: the domain name expected as the TLS certificate's commonname,
           defaults to smtphost.
    """
    msg = BytesIO(msg_bytes)
    d: "Deferred[object]" = Deferred()
    if not enable_tls:
        tlsname = None
    elif tlsname is None:
        tlsname = smtphost

    factory: IProtocolFactory = ESMTPSenderFactory(
        username,
        password,
        from_addr,
        to_addr,
        msg,
        d,
        heloFallback=True,
        requireAuthentication=require_auth,
        requireTransportSecurity=require_tls,
        hostname=tlsname,
    )

    if force_tls:
        factory = TLSMemoryBIOFactory(optionsForClientTLS(tlsname), True, factory)

    endpoint = HostnameEndpoint(
        reactor, smtphost, smtpport, timeout=30, bindAddress=None
    )

    await make_deferred_yieldable(endpoint.connect(factory))

    await make_deferred_yieldable(d)


class SendEmailHandler:
    def __init__(self, hs: "HomeServer"):
        self.hs = hs

        self._reactor = hs.get_reactor()

        self._from = hs.config.email.email_notif_from
        self._smtp_host = hs.config.email.email_smtp_host
        self._smtp_port = hs.config.email.email_smtp_port

        user = hs.config.email.email_smtp_user
        self._smtp_user = user.encode("utf-8") if user is not None else None
        passwd = hs.config.email.email_smtp_pass
        self._smtp_pass = passwd.encode("utf-8") if passwd is not None else None
        self._require_transport_security = hs.config.email.require_transport_security
        self._enable_tls = hs.config.email.enable_smtp_tls
        self._force_tls = hs.config.email.force_tls
        self._tlsname = hs.config.email.email_tlsname

        self._sendmail = _sendmail

    async def send_email(
        self,
        email_address: str,
        subject: str,
        app_name: str,
        html: str,
        text: str,
        additional_headers: dict[str, str] | None = None,
    ) -> None:
        """Send a multipart email with the given information.

        Args:
            email_address: The address to send the email to.
            subject: The email's subject.
            app_name: The app name to include in the From header.
            html: The HTML content to include in the email.
            text: The plain text content to include in the email.
            additional_headers: A map of additional headers to include.
        """
        try:
            from_string = self._from % {"app": app_name}  # type: ignore[operator]
        except (KeyError, TypeError):
            from_string = self._from

        raw_from = email.utils.parseaddr(from_string)[1]
        raw_to = email.utils.parseaddr(email_address)[1]

        if raw_to == "":
            raise RuntimeError("Invalid 'to' address")

        html_part = MIMEText(html, "html", "utf-8")
        text_part = MIMEText(text, "plain", "utf-8")

        multipart_msg = MIMEMultipart("alternative")
        multipart_msg["Subject"] = subject
        multipart_msg["From"] = from_string
        multipart_msg["To"] = email_address
        multipart_msg["Date"] = email.utils.formatdate()
        multipart_msg["Message-ID"] = email.utils.make_msgid()

        # Discourage automatic responses to Synapse's emails.
        # Per RFC 3834, automatic responses should not be sent if the "Auto-Submitted"
        # header is present with any value other than "no". See
        #     https://www.rfc-editor.org/rfc/rfc3834.html#section-5.1
        multipart_msg["Auto-Submitted"] = "auto-generated"
        # Also include a Microsoft-Exchange specific header:
        #    https://learn.microsoft.com/en-us/openspecs/exchange_server_protocols/ms-oxcmail/ced68690-498a-4567-9d14-5c01f974d8b1
        # which suggests it can take the value "All" to "suppress all auto-replies",
        # or a comma separated list of auto-reply classes to suppress.
        # The following stack overflow question has a little more context:
        #    https://stackoverflow.com/a/25324691/5252017
        #    https://stackoverflow.com/a/61646381/5252017
        multipart_msg["X-Auto-Response-Suppress"] = "All"

        if additional_headers:
            for header, value in additional_headers.items():
                multipart_msg[header] = value

        multipart_msg.attach(text_part)
        multipart_msg.attach(html_part)

        logger.info("Sending email to %s", email_address)

        await self._sendmail(
            self._reactor,
            self._smtp_host,
            self._smtp_port,
            raw_from,
            raw_to,
            multipart_msg.as_string().encode("utf8"),
            username=self._smtp_user,
            password=self._smtp_pass,
            require_auth=self._smtp_user is not None,
            require_tls=self._require_transport_security,
            enable_tls=self._enable_tls,
            force_tls=self._force_tls,
            tlsname=self._tlsname,
        )
