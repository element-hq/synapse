from synapse.storage.invite_rule import InviteRule, InviteRulesConfig
from synapse.types import UserID

from tests import unittest

regular_user = UserID.from_string("@test:example.org")
allowed_user = UserID.from_string("@allowed:allow.example.org")
blocked_user = UserID.from_string("@blocked:block.example.org")
ignored_user = UserID.from_string("@ignored:ignore.example.org")


class InviteFilterTestCase(unittest.TestCase):
    def test_empty(self) -> None:
        """Permit by default"""
        config = InviteRulesConfig(None)
        self.assertEqual(
            config.get_invite_rule(regular_user.to_string()), InviteRule.ALLOW
        )

    def test_ignore_invalid(self) -> None:
        """Invalid strings are ignored"""
        config = InviteRulesConfig({"blocked_users": ["not a user"]})
        self.assertEqual(
            config.get_invite_rule(blocked_user.to_string()), InviteRule.ALLOW
        )

    def test_user_blocked(self) -> None:
        """Permit all, except explicitly blocked users"""
        config = InviteRulesConfig({"blocked_users": [blocked_user.to_string()]})
        self.assertEqual(
            config.get_invite_rule(blocked_user.to_string()), InviteRule.BLOCK
        )
        self.assertEqual(
            config.get_invite_rule(regular_user.to_string()), InviteRule.ALLOW
        )

    def test_user_ignored(self) -> None:
        """Permit all, except explicitly ignored users"""
        config = InviteRulesConfig({"ignored_users": [ignored_user.to_string()]})
        self.assertEqual(
            config.get_invite_rule(ignored_user.to_string()), InviteRule.IGNORE
        )
        self.assertEqual(
            config.get_invite_rule(regular_user.to_string()), InviteRule.ALLOW
        )

    def test_user_precedence(self) -> None:
        """Always take allowed over ignored, ignored over blocked, and then block."""
        config = InviteRulesConfig(
            {
                "allowed_users": [allowed_user.to_string()],
                "ignored_users": [allowed_user.to_string(), ignored_user.to_string()],
                "blocked_users": [
                    allowed_user.to_string(),
                    ignored_user.to_string(),
                    blocked_user.to_string(),
                ],
            }
        )
        self.assertEqual(
            config.get_invite_rule(allowed_user.to_string()), InviteRule.ALLOW
        )
        self.assertEqual(
            config.get_invite_rule(ignored_user.to_string()), InviteRule.IGNORE
        )
        self.assertEqual(
            config.get_invite_rule(blocked_user.to_string()), InviteRule.BLOCK
        )

    def test_server_blocked(self) -> None:
        """Block all users on the server except those allowed."""
        user_on_same_server = UserID("blocked", allowed_user.domain)
        config = InviteRulesConfig(
            {
                "allowed_users": [allowed_user.to_string()],
                "blocked_servers": [allowed_user.domain],
            }
        )
        self.assertEqual(
            config.get_invite_rule(allowed_user.to_string()), InviteRule.ALLOW
        )
        self.assertEqual(
            config.get_invite_rule(user_on_same_server.to_string()), InviteRule.BLOCK
        )

    def test_server_ignored(self) -> None:
        """Ignore all users on the server except those allowed."""
        user_on_same_server = UserID("ignored", allowed_user.domain)
        config = InviteRulesConfig(
            {
                "allowed_users": [allowed_user.to_string()],
                "ignored_servers": [allowed_user.domain],
            }
        )
        self.assertEqual(
            config.get_invite_rule(allowed_user.to_string()), InviteRule.ALLOW
        )
        self.assertEqual(
            config.get_invite_rule(user_on_same_server.to_string()), InviteRule.IGNORE
        )

    def test_server_allow(self) -> None:
        """Allow all from a server except explictly blocked or ignored users."""
        blocked_user_on_same_server = UserID("blocked", allowed_user.domain)
        ignored_user_on_same_server = UserID("ignored", allowed_user.domain)
        allowed_user_on_same_server = UserID("another", allowed_user.domain)
        config = InviteRulesConfig(
            {
                "ignored_users": [ignored_user_on_same_server.to_string()],
                "blocked_users": [blocked_user_on_same_server.to_string()],
                "allowed_servers": [allowed_user.to_string()],
            }
        )
        self.assertEqual(
            config.get_invite_rule(allowed_user.to_string()), InviteRule.ALLOW
        )
        self.assertEqual(
            config.get_invite_rule(allowed_user_on_same_server.to_string()),
            InviteRule.ALLOW,
        )
        self.assertEqual(
            config.get_invite_rule(blocked_user_on_same_server.to_string()),
            InviteRule.BLOCK,
        )
        self.assertEqual(
            config.get_invite_rule(ignored_user_on_same_server.to_string()),
            InviteRule.IGNORE,
        )

    def test_server_precedence(self) -> None:
        """Always take allowed over ignored, ignored over blocked, and then block."""
        config = InviteRulesConfig(
            {
                "allowed_servers": [allowed_user.domain],
                "ignored_servers": [allowed_user.domain, ignored_user.domain],
                "blocked_servers": [
                    allowed_user.domain,
                    ignored_user.domain,
                    blocked_user.domain,
                ],
            }
        )
        self.assertEqual(
            config.get_invite_rule(allowed_user.to_string()), InviteRule.ALLOW
        )
        self.assertEqual(
            config.get_invite_rule(ignored_user.to_string()), InviteRule.IGNORE
        )
        self.assertEqual(
            config.get_invite_rule(blocked_user.to_string()), InviteRule.BLOCK
        )

    def test_server_glob(self) -> None:
        """Test that glob patterns match"""
        config = InviteRulesConfig({"blocked_servers": ["*.example.org"]})
        self.assertEqual(
            config.get_invite_rule(allowed_user.to_string()), InviteRule.BLOCK
        )
        self.assertEqual(
            config.get_invite_rule(ignored_user.to_string()), InviteRule.BLOCK
        )
        self.assertEqual(
            config.get_invite_rule(blocked_user.to_string()), InviteRule.BLOCK
        )
        self.assertEqual(
            config.get_invite_rule(regular_user.to_string()), InviteRule.ALLOW
        )
