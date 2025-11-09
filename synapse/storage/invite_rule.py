import logging
from enum import Enum
from typing import Pattern

from matrix_common.regex import glob_to_regex

from synapse.types import JsonMapping, UserID

logger = logging.getLogger(__name__)


class InviteRule(Enum):
    """Enum to define the action taken when an invite matches a rule."""

    ALLOW = "allow"
    BLOCK = "block"
    IGNORE = "ignore"


class InviteRulesConfig:
    """Class to determine if a given user permits an invite from another user, and the action to take."""

    def __init__(self, account_data: JsonMapping | None):
        self.allowed_users: list[Pattern[str]] = []
        self.ignored_users: list[Pattern[str]] = []
        self.blocked_users: list[Pattern[str]] = []

        self.allowed_servers: list[Pattern[str]] = []
        self.ignored_servers: list[Pattern[str]] = []
        self.blocked_servers: list[Pattern[str]] = []

        def process_field(
            values: list[str] | None,
            ruleset: list[Pattern[str]],
            rule: InviteRule,
        ) -> None:
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, str) and len(value) > 0:
                        # User IDs cannot exceed 255 bytes. Don't process large, potentially
                        # expensive glob patterns.
                        if len(value) > 255:
                            logger.debug(
                                "Ignoring invite config glob pattern that is >255 bytes: {value}"
                            )
                            continue

                        try:
                            ruleset.append(glob_to_regex(value))
                        except Exception as e:
                            # If for whatever reason we can't process this, just ignore it.
                            logger.debug(
                                "Could not process '%s' field of invite rule config, ignoring: %s",
                                value,
                                e,
                            )

        if account_data:
            process_field(
                account_data.get("allowed_users"), self.allowed_users, InviteRule.ALLOW
            )
            process_field(
                account_data.get("ignored_users"), self.ignored_users, InviteRule.IGNORE
            )
            process_field(
                account_data.get("blocked_users"), self.blocked_users, InviteRule.BLOCK
            )
            process_field(
                account_data.get("allowed_servers"),
                self.allowed_servers,
                InviteRule.ALLOW,
            )
            process_field(
                account_data.get("ignored_servers"),
                self.ignored_servers,
                InviteRule.IGNORE,
            )
            process_field(
                account_data.get("blocked_servers"),
                self.blocked_servers,
                InviteRule.BLOCK,
            )

    def get_invite_rule(self, user_id: str) -> InviteRule:
        """Get the invite rule that matches this user. Will return InviteRule.ALLOW if no rules match

        Args:
            user_id: The user ID of the inviting user.

        """
        user = UserID.from_string(user_id)
        # The order here is important. We always process user rules before server rules
        # and we always process in the order of Allow, Ignore, Block.
        for patterns, rule in [
            (self.allowed_users, InviteRule.ALLOW),
            (self.ignored_users, InviteRule.IGNORE),
            (self.blocked_users, InviteRule.BLOCK),
        ]:
            for regex in patterns:
                if regex.match(user_id):
                    return rule

        for patterns, rule in [
            (self.allowed_servers, InviteRule.ALLOW),
            (self.ignored_servers, InviteRule.IGNORE),
            (self.blocked_servers, InviteRule.BLOCK),
        ]:
            for regex in patterns:
                if regex.match(user.domain):
                    return rule

        return InviteRule.ALLOW
