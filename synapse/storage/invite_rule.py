from enum import Enum
from typing import Dict, Optional, Pattern

from matrix_common.regex import glob_to_regex

from synapse.types import JsonMapping, UserID


class InviteRule(Enum):
    """Enum to define the action taken when an invite matches a rule."""

    ALLOW = "allow"
    BLOCK = "block"
    IGNORE = "ignore"


class InviteRulesConfig:
    """Class to determine if a given user permits an invite from another user, and the action to take."""

    user_rules: Dict[str, InviteRule]
    server_rules: Dict[Pattern[str], InviteRule]

    def __init__(
        self,
        account_data: Optional[JsonMapping],
        always_allow_user_id: Optional[str] = None,
    ):
        self.user_rules = {}
        self.server_rules = {}

        if not account_data:
            # If there is no account data, then this effectively means allow all invites.
            return

        # In reverse order of importance.
        for user_id in account_data.get("blocked_users", []):
            if not UserID.is_valid(user_id):
                continue
            self.user_rules[user_id] = InviteRule.BLOCK

        for user_id in account_data.get("ignored_users", []):
            if not UserID.is_valid(user_id):
                continue
            self.user_rules[user_id] = InviteRule.IGNORE

        for user_id in account_data.get("allowed_users", []):
            if not UserID.is_valid(user_id):
                continue
            self.user_rules[user_id] = InviteRule.ALLOW

        # If server notices are configured, force enable this user.
        if always_allow_user_id:
            self.user_rules[always_allow_user_id] = InviteRule.ALLOW

        def process_server_rule(server_name, rule: InviteRule) -> None:
            if not isinstance(server_name, str) or len(server_name) < 1:
                return
            regex = glob_to_regex(server_name)
            self.server_rules[regex] = rule

        for server_name in account_data.get("blocked_servers", []):
            process_server_rule(server_name, InviteRule.BLOCK)

        for server_name in account_data.get("ignored_servers", []):
            process_server_rule(server_name, InviteRule.IGNORE)

        for server_name in account_data.get("allowed_servers", []):
            process_server_rule(server_name, InviteRule.ALLOW)

    def get_invite_rule(self, user_id: UserID) -> InviteRule:
        """Get the invite rule that matches this user. Will return InviteRule.ALLOW if no rules match"""
        user_rule = self.user_rules.get(user_id.to_string())
        if user_rule:
            return user_rule

        for regex, server_rule in self.server_rules.items():
            if regex.match(user_id.domain):
                return server_rule

        return InviteRule.ALLOW
