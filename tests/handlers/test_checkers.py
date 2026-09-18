from typing import Dict
from unittest.mock import AsyncMock, Mock

from twisted.internet.defer import ensureDeferred
from twisted.test.proto_helpers import MemoryReactor
from twisted.trial import unittest
from twisted.web.client import PartialDownloadError

from synapse.api.errors import Codes, LoginError
from synapse.handlers.ui_auth.checkers import RecaptchaAuthChecker
from synapse.server import HomeServer
from synapse.util import json_encoder


class TestRecaptchaAuthChecker(unittest.TestCase):
    def setUp(self: "TestRecaptchaAuthChecker") -> None:
        self.hs = Mock(spec=HomeServer)
        self.hs.config = Mock()
        self.hs.config.captcha.recaptcha_private_key = "test_private_key"
        self.hs.config.captcha.recaptcha_siteverify_api = (
            "https://www.recaptcha.net/recaptcha/api/siteverify"
        )
        self.http_client = AsyncMock()
        self.hs.get_proxied_http_client.return_value = self.http_client
        self.recaptcha_checker = RecaptchaAuthChecker(self.hs)
        self.reactor = MemoryReactor()

    def test_is_enabled(self: "TestRecaptchaAuthChecker") -> None:
        """Test that the checker is enabled when a private key is configured."""
        self.assertTrue(self.recaptcha_checker.is_enabled())

    def test_is_disabled(self: "TestRecaptchaAuthChecker") -> None:
        """Test that the checker is disabled when no private key is configured."""
        self.hs.config.captcha.recaptcha_private_key = None
        recaptcha_checker = RecaptchaAuthChecker(self.hs)
        self.assertFalse(recaptcha_checker.is_enabled())

    def test_check_auth_success(self: "TestRecaptchaAuthChecker") -> None:
        """Test that authentication succeeds with a valid recaptcha response."""
        _expected_response = {"success": True}
        self.http_client.post_urlencoded_get_json = AsyncMock(
            return_value=_expected_response
        )

        authdict = {"response": "captcha_solution", "session": "fake_session_id"}
        clientip = "127.0.0.1"

        d = ensureDeferred(self.recaptcha_checker.check_auth(authdict, clientip))

        result = self.successResultOf(d)
        self.assertTrue(result)

        self.http_client.post_urlencoded_get_json.assert_called_once_with(
            self.hs.config.captcha.recaptcha_siteverify_api,
            args={
                "secret": "test_private_key",
                "response": "captcha_solution",
                "remoteip": clientip,
            },
        )

    def test_check_auth_failure(self: "TestRecaptchaAuthChecker") -> None:
        """Test that authentication fails with an invalid recaptcha response."""
        _expected_response = {"success": False}
        self.http_client.post_urlencoded_get_json = AsyncMock(
            return_value=_expected_response
        )

        authdict = {"response": "invalid_response", "session": "fake_session_id"}
        clientip = "127.0.0.1"

        d = ensureDeferred(self.recaptcha_checker.check_auth(authdict, clientip))
        f = self.failureResultOf(d, LoginError)
        self.assertEqual(f.value.errcode, Codes.UNAUTHORIZED)

    def test_check_missing_session(self: "TestRecaptchaAuthChecker") -> None:
        """Test that authentication fails when the session ID is missing."""

        authdict = {"response": "invalid_response"}
        clientip = "127.0.0.1"

        d = ensureDeferred(self.recaptcha_checker.check_auth(authdict, clientip))
        f = self.failureResultOf(d, LoginError)
        self.assertEqual(f.value.errcode, Codes.UNAUTHORIZED)

    def test_check_auth_missing_response(self: "TestRecaptchaAuthChecker") -> None:
        """Test that authentication fails when the user captcha response is missing."""
        authdict: Dict[str, str] = {}
        clientip = "127.0.0.1"

        d = ensureDeferred(self.recaptcha_checker.check_auth(authdict, clientip))
        f = self.failureResultOf(d, LoginError)
        self.assertEqual(f.value.errcode, Codes.CAPTCHA_NEEDED)

    def test_check_auth_exception(self: "TestRecaptchaAuthChecker") -> None:
        """Test that authentication succeeds when a PartialDownloadError occurs during verification."""

        partial_download_error = PartialDownloadError(500)
        partial_download_error.response = json_encoder.encode({"success": True}).encode(
            "utf-8"
        )
        self.http_client.post_urlencoded_get_json.side_effect = partial_download_error

        authdict = {"response": "captcha_solution", "session": "fake_session_id"}
        clientip = "127.0.0.1"

        d = ensureDeferred(self.recaptcha_checker.check_auth(authdict, clientip))
        result = self.successResultOf(d)
        self.assertTrue(result)

    def test_check_auth_logging(self: "TestRecaptchaAuthChecker") -> None:
        """Test that the client IP is not logged during authentication."""
        with self.assertLogs(level="INFO") as cm:
            authdict = {"response": "captcha_solution", "session": "fake_session_id"}
            clientip = "127.0.0.1"

            _expected_response = {"success": True}
            self.http_client.post_urlencoded_get_json = AsyncMock(
                return_value=_expected_response
            )

            d = ensureDeferred(self.recaptcha_checker.check_auth(authdict, clientip))
            self.successResultOf(d)

        logs = "\n".join(cm.output)
        self.assertNotIn(clientip, logs)
