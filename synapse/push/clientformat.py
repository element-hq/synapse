#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016 OpenMarket Ltd
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

import copy
from typing import Any

from synapse.push.rulekinds import PRIORITY_CLASS_INVERSE_MAP, PRIORITY_CLASS_MAP
from synapse.synapse_rust.push import FilteredPushRules, PushRule
from synapse.types import UserID


def format_push_rules_for_user(
    user: UserID, ruleslist: FilteredPushRules
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Converts a list of rawrules and a enabled map into nested dictionaries
    to match the Matrix client-server format for push rules"""

    rules: dict[str, dict[str, list[dict[str, Any]]]] = {"global": {}}

    rules["global"] = _add_empty_priority_class_arrays(rules["global"])

    for r, enabled in ruleslist.rules():
        template_name = _priority_class_to_template_name(r.priority_class)

        rulearray = rules["global"][template_name]

        template_rule = _rule_to_template(r)
        if not template_rule:
            continue

        rulearray.append(template_rule)

        _convert_type_to_value(template_rule, user)

        template_rule["enabled"] = enabled

        if "conditions" not in template_rule:
            # Not all formatted rules have explicit conditions, e.g. "room"
            # rules omit them as they can be derived from the kind and rule ID.
            #
            # If the formatted rule has no conditions then we can skip the
            # formatting of conditions.
            continue

        # Remove internal stuff.
        template_rule["conditions"] = copy.deepcopy(template_rule["conditions"])
        for c in template_rule["conditions"]:
            c.pop("_cache_key", None)

            _convert_type_to_value(c, user)

    return rules


def _convert_type_to_value(rule_or_cond: dict[str, Any], user: UserID) -> None:
    for type_key in ("pattern", "value"):
        type_value = rule_or_cond.pop(f"{type_key}_type", None)
        if type_value == "user_id":
            rule_or_cond[type_key] = user.to_string()
        elif type_value == "user_localpart":
            rule_or_cond[type_key] = user.localpart


def _add_empty_priority_class_arrays(d: dict[str, list]) -> dict[str, list]:
    for pc in PRIORITY_CLASS_MAP.keys():
        d[pc] = []
    return d


def _rule_to_template(rule: PushRule) -> dict[str, Any] | None:
    templaterule: dict[str, Any]

    unscoped_rule_id = _rule_id_from_namespaced(rule.rule_id)

    template_name = _priority_class_to_template_name(rule.priority_class)
    if template_name in ["override", "underride", "postcontent"]:
        templaterule = {"conditions": rule.conditions, "actions": rule.actions}
    elif template_name in ["sender", "room"]:
        templaterule = {"actions": rule.actions}
        unscoped_rule_id = rule.conditions[0]["pattern"]
    elif template_name == "content":
        if len(rule.conditions) != 1:
            return None
        thecond = rule.conditions[0]

        templaterule = {"actions": rule.actions}
        if "pattern" in thecond:
            templaterule["pattern"] = thecond["pattern"]
        elif "pattern_type" in thecond:
            templaterule["pattern_type"] = thecond["pattern_type"]
        else:
            return None
    else:
        # This should not be reached unless this function is not kept in sync
        # with PRIORITY_CLASS_INVERSE_MAP.
        raise ValueError("Unexpected template_name: %s" % (template_name,))

    templaterule["rule_id"] = unscoped_rule_id
    templaterule["default"] = rule.default
    return templaterule


def _rule_id_from_namespaced(in_rule_id: str) -> str:
    return in_rule_id.split("/")[-1]


def _priority_class_to_template_name(pc: int) -> str:
    return PRIORITY_CLASS_INVERSE_MAP[pc]
