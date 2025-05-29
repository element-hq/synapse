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

    user_rules: ImmutableOrderedDict[Pattern[str], InviteRule]
    server_rules: ImmutableOrderedDict[Pattern[str], InviteRule]

    def __init__(self, account_data: Optional[JsonMapping]):
        user_rules: dict[Pattern[str], InviteRule] = {}
        server_rules: dict[Pattern[str], InviteRule] = {}

        def process_field(field: str, ruleset: dict[Pattern[str], InviteRule], rule: InviteRule) -> None:
            values = account_data.get(field)
            if not isinstance(values, list):
                return
            for value in values:
                if not isinstance(value, str) or len(value) < 1:
                    return
                try:
                    ruleset[glob_to_regex(value)] = rule
                except:
                    pass

        if account_data:
            # In reverse order of importance.
            process_field("blocked_users", user_rules, InviteRule.BLOCK)
            process_field("ignored_users", user_rules, InviteRule.IGNORE)
            process_field("allowed_users", user_rules, InviteRule.ALLOW)
            process_field("blocked_servers", server_rules, InviteRule.BLOCK)
            process_field("ignored_servers", server_rules, InviteRule.IGNORE)
            process_field("allowed_servers", server_rules, InviteRule.ALLOW)

        self.user_rules = ImmutableOrderedDict(user_rules)
        self.server_rules = ImmutableOrderedDict(server_rules)

    def get_invite_rule(self, user_id: UserID) -> InviteRule:
        """Get the invite rule that matches this user. Will return InviteRule.ALLOW if no rules match"""
        for regex, user_rule in self.user_rules.items():
            if regex.match(user_id.to_string()):
                return user_rule

        for regex, server_rule in self.server_rules.items():
            if regex.match(user_id.domain):
                return server_rule

        return InviteRule.ALLOW
