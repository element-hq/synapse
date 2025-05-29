from enum import Enum
from typing import Optional, Pattern

from immutabledict import ImmutableOrderedDict
from matrix_common.regex import glob_to_regex

from synapse.types import JsonMapping, UserID


class InviteRule(Enum):
    """Enum to define the action taken when an invite matches a rule."""

    ALLOW = "allow"
    BLOCK = "block"
    IGNORE = "ignore"


class InviteRulesConfig:
    """Class to determine if a given user permits an invite from another user, and the action to take."""

    user_rules: ImmutableOrderedDict[str, InviteRule]
    server_rules: ImmutableOrderedDict[Pattern[str], InviteRule]

    def __init__(self, account_data: Optional[JsonMapping]):
        user_rules: dict[str, InviteRule] = {}
        server_rules: dict[Pattern[str], InviteRule] = {}

        if not account_data:
            self.user_rules = ImmutableOrderedDict({})
            self.server_rules = ImmutableOrderedDict({})
            # If there is no account data, then this effectively means allow all invites.
            return

        # In reverse order of importance.
        for user_id in account_data.get("blocked_users", []):
            if not UserID.is_valid(user_id):
                continue
            user_rules[user_id] = InviteRule.BLOCK

        for user_id in account_data.get("ignored_users", []):
            if not UserID.is_valid(user_id):
                continue
            user_rules[user_id] = InviteRule.IGNORE

        for user_id in account_data.get("allowed_users", []):
            if not UserID.is_valid(user_id):
                continue
            user_rules[user_id] = InviteRule.ALLOW

        def process_server_rule(server_name: str, rule: InviteRule) -> None:
            if not isinstance(server_name, str) or len(server_name) < 1:
                return
            regex = glob_to_regex(server_name)
            server_rules[regex] = rule

        for server_name in account_data.get("blocked_servers", []):
            process_server_rule(server_name, InviteRule.BLOCK)

        for server_name in account_data.get("ignored_servers", []):
            process_server_rule(server_name, InviteRule.IGNORE)

        for server_name in account_data.get("allowed_servers", []):
            process_server_rule(server_name, InviteRule.ALLOW)

        self.user_rules = ImmutableOrderedDict(user_rules)
        self.server_rules = ImmutableOrderedDict(server_rules)

    def get_invite_rule(self, user_id: UserID) -> InviteRule:
        """Get the invite rule that matches this user. Will return InviteRule.ALLOW if no rules match"""
        user_rule = self.user_rules.get(user_id.to_string())
        if user_rule:
            return user_rule

        for regex, server_rule in self.server_rules.items():
            if regex.match(user_id.domain):
                return server_rule

        return InviteRule.ALLOW
