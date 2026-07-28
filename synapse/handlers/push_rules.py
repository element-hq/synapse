#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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
from typing import TYPE_CHECKING, Any

import attr

from synapse.api.errors import SynapseError, UnrecognizedRequestError
from synapse.push.clientformat import format_push_rules_for_user
from synapse.storage.push_rule import RuleNotFoundException
from synapse.synapse_rust.push import get_base_rule_ids
from synapse.types import JsonDict, StreamKeyType, UserID

if TYPE_CHECKING:
    from synapse.server import HomeServer


BASE_RULE_IDS = get_base_rule_ids()


@attr.s(slots=True, frozen=True, auto_attribs=True)
class RuleSpec:
    scope: str
    template: str
    rule_id: str
    attr: str | None


class PushRulesHandler:
    """A class to handle changes in push rules for users."""

    def __init__(self, hs: "HomeServer"):
        self._notifier = hs.get_notifier()
        self._main_store = hs.get_datastores().main

    async def set_rule_attr(
        self, user_id: str, spec: RuleSpec, val: bool | JsonDict
    ) -> None:
        """Set an attribute (enabled or actions) on an existing push rule.

        Notifies listeners (e.g. sync handler) of the change.

        Args:
            user_id: the user for which to modify the push rule.
            spec: the spec of the push rule to modify.
            val: the value to change the attribute to.

        Raises:
            RuleNotFoundException if the rule being modified doesn't exist.
            SynapseError(400) if the value is malformed.
            UnrecognizedRequestError if the attribute to change is unknown.
            InvalidRuleException if we're trying to change the actions on a rule but
                the provided actions aren't compliant with the spec.
        """
        if spec.attr not in ("enabled", "actions"):
            # for the sake of potential future expansion, shouldn't report
            # 404 in the case of an unknown request so check it corresponds to
            # a known attribute first.
            raise UnrecognizedRequestError()

        namespaced_rule_id = f"global/{spec.template}/{spec.rule_id}"
        rule_id = spec.rule_id
        is_default_rule = rule_id.startswith(".")
        if is_default_rule:
            if namespaced_rule_id not in BASE_RULE_IDS:
                raise RuleNotFoundException("Unknown rule %r" % (namespaced_rule_id,))
        if spec.attr == "enabled":
            if isinstance(val, dict) and "enabled" in val:
                val = val["enabled"]
            if not isinstance(val, bool):
                # Legacy fallback
                # This should *actually* take a dict, but many clients pass
                # bools directly, so let's not break them.
                raise SynapseError(400, "Value for 'enabled' must be boolean")
            await self._main_store.set_push_rule_enabled(
                user_id, namespaced_rule_id, val, is_default_rule
            )
        elif spec.attr == "actions":
            if not isinstance(val, dict):
                raise SynapseError(400, "Value must be a dict")
            actions = val.get("actions")
            if not isinstance(actions, list):
                raise SynapseError(400, "Value for 'actions' must be dict")
            check_actions(actions)
            rule_id = spec.rule_id
            is_default_rule = rule_id.startswith(".")
            if is_default_rule:
                if namespaced_rule_id not in BASE_RULE_IDS:
                    raise RuleNotFoundException(
                        "Unknown rule %r" % (namespaced_rule_id,)
                    )
            await self._main_store.set_push_rule_actions(
                user_id, namespaced_rule_id, actions, is_default_rule
            )
        else:
            raise UnrecognizedRequestError()

        self.notify_user(user_id)

    def notify_user(self, user_id: str) -> None:
        """Notify listeners about a push rule change.

        Args:
            user_id: the user ID the change is for.
        """
        stream_id = self._main_store.get_max_push_rules_stream_id()
        self._notifier.on_new_event(
            StreamKeyType.PUSH_RULES, stream_id, users=[user_id]
        )

    async def push_rules_for_user(
        self, user: UserID
    ) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """
        Push rules aren't really account data, but get formatted as such for /sync.
        """
        user_id = user.to_string()
        rules_raw = await self._main_store.get_push_rules_for_user(user_id)
        rules = format_push_rules_for_user(user, rules_raw)
        return rules


def check_actions(actions: list[str | JsonDict]) -> None:
    """Check if the given actions are spec compliant.

    Args:
        actions: the actions to check.

    Raises:
        InvalidRuleException if the rules aren't compliant with the spec.
    """
    if not isinstance(actions, list):
        raise InvalidRuleException("No actions found")

    for a in actions:
        # "dont_notify" and "coalesce" are legacy actions. They are allowed, but
        # ignored (resulting in no action from the pusher).
        if a in ["notify", "dont_notify", "coalesce"]:
            pass
        elif isinstance(a, dict) and "set_tweak" in a:
            pass
        else:
            raise InvalidRuleException("Unrecognised action %s" % a)


class InvalidRuleException(Exception):
    pass
