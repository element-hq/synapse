#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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

from typing import TYPE_CHECKING, List, Tuple, Union

from synapse.api.errors import (
    NotFoundError,
    StoreError,
    SynapseError,
    UnrecognizedRequestError,
)
from synapse.handlers.push_rules import InvalidRuleException, RuleSpec, check_actions
from synapse.http.server import HttpServer
from synapse.http.servlet import (
    RestServlet,
    parse_json_value_from_request,
    parse_string,
)
from synapse.http.site import SynapseRequest
from synapse.push.rulekinds import PRIORITY_CLASS_MAP
from synapse.rest.client._base import client_patterns
from synapse.storage.push_rule import InconsistentRuleException, RuleNotFoundException
from synapse.types import JsonDict
from synapse.util.async_helpers import Linearizer

if TYPE_CHECKING:
    from synapse.server import HomeServer


class PushRuleRestServlet(RestServlet):
    PATTERNS = client_patterns("/(?P<path>pushrules/.*)$", v1=True)
    SLIGHTLY_PEDANTIC_TRAILING_SLASH_ERROR = (
        "Unrecognised request: You probably wanted a trailing slash"
    )

    WORKERS_DENIED_METHODS = ["PUT", "DELETE"]
    CATEGORY = "Push rule requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self.notifier = hs.get_notifier()
        self._is_push_worker = (
            hs.get_instance_name() in hs.config.worker.writers.push_rules
        )
        self._push_rules_handler = hs.get_push_rules_handler()
        self._push_rule_linearizer = Linearizer(name="push_rules")

    async def on_PUT(self, request: SynapseRequest, path: str) -> Tuple[int, JsonDict]:
        if not self._is_push_worker:
            raise Exception("Cannot handle PUT /push_rules on worker")

        requester = await self.auth.get_user_by_req(request)
        user_id = requester.user.to_string()

        async with self._push_rule_linearizer.queue(user_id):
            return await self.handle_put(request, path, user_id)

    async def handle_put(
        self, request: SynapseRequest, path: str, user_id: str
    ) -> Tuple[int, JsonDict]:
        spec = _rule_spec_from_path(path.split("/"))
        try:
            priority_class = _priority_class_from_spec(spec)
        except InvalidRuleException as e:
            raise SynapseError(400, str(e))

        if "/" in spec.rule_id or "\\" in spec.rule_id:
            raise SynapseError(400, "rule_id may not contain slashes")

        content = parse_json_value_from_request(request)

        if spec.attr:
            try:
                await self._push_rules_handler.set_rule_attr(user_id, spec, content)
            except InvalidRuleException as e:
                raise SynapseError(400, "Invalid actions: %s" % e)
            except RuleNotFoundException:
                raise NotFoundError("Unknown rule")

            return 200, {}

        if spec.rule_id.startswith("."):
            # Rule ids starting with '.' are reserved for server default rules.
            raise SynapseError(400, "cannot add new rule_ids that start with '.'")

        try:
            (conditions, actions) = _rule_tuple_from_request_object(
                spec.template, spec.rule_id, content
            )
        except InvalidRuleException as e:
            raise SynapseError(400, str(e))

        before = parse_string(request, "before")
        if before:
            before = f"global/{spec.template}/{before}"

        after = parse_string(request, "after")
        if after:
            after = f"global/{spec.template}/{after}"

        try:
            await self.store.add_push_rule(
                user_id=user_id,
                rule_id=f"global/{spec.template}/{spec.rule_id}",
                priority_class=priority_class,
                conditions=conditions,
                actions=actions,
                before=before,
                after=after,
            )
            self._push_rules_handler.notify_user(user_id)
        except InconsistentRuleException as e:
            raise SynapseError(400, str(e))
        except RuleNotFoundException as e:
            raise SynapseError(400, str(e))

        return 200, {}

    async def on_DELETE(
        self, request: SynapseRequest, path: str
    ) -> Tuple[int, JsonDict]:
        if not self._is_push_worker:
            raise Exception("Cannot handle DELETE /push_rules on worker")

        requester = await self.auth.get_user_by_req(request)
        user_id = requester.user.to_string()

        async with self._push_rule_linearizer.queue(user_id):
            return await self.handle_delete(request, path, user_id)

    async def handle_delete(
        self,
        request: SynapseRequest,
        path: str,
        user_id: str,
    ) -> Tuple[int, JsonDict]:
        spec = _rule_spec_from_path(path.split("/"))

        namespaced_rule_id = f"global/{spec.template}/{spec.rule_id}"

        try:
            await self.store.delete_push_rule(user_id, namespaced_rule_id)
            self._push_rules_handler.notify_user(user_id)
            return 200, {}
        except StoreError as e:
            if e.code == 404:
                raise NotFoundError()
            else:
                raise

    async def on_GET(self, request: SynapseRequest, path: str) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        requester.user.to_string()

        # we build up the full structure and then decide which bits of it
        # to send which means doing unnecessary work sometimes but is
        # is probably not going to make a whole lot of difference
        rules = await self._push_rules_handler.push_rules_for_user(requester.user)

        path_parts = path.split("/")[1:]

        if path_parts == []:
            # we're a reference impl: pedantry is our job.
            raise UnrecognizedRequestError(
                PushRuleRestServlet.SLIGHTLY_PEDANTIC_TRAILING_SLASH_ERROR
            )

        if path_parts[0] == "":
            return 200, rules
        elif path_parts[0] == "global":
            result = _filter_ruleset_with_path(rules["global"], path_parts[1:])
            return 200, result
        else:
            raise UnrecognizedRequestError()


def _rule_spec_from_path(path: List[str]) -> RuleSpec:
    """Turn a sequence of path components into a rule spec

    Args:
        path: the URL path components.

    Returns:
        rule spec, containing scope/template/rule_id entries, and possibly attr.

    Raises:
        UnrecognizedRequestError if the path components cannot be parsed.
    """
    if len(path) < 2:
        raise UnrecognizedRequestError()
    if path[0] != "pushrules":
        raise UnrecognizedRequestError()

    scope = path[1]
    path = path[2:]
    if scope != "global":
        raise UnrecognizedRequestError()

    if len(path) == 0:
        raise UnrecognizedRequestError()

    template = path[0]
    path = path[1:]

    if len(path) == 0 or len(path[0]) == 0:
        raise UnrecognizedRequestError()

    rule_id = path[0]

    path = path[1:]

    attr = None
    if len(path) > 0 and len(path[0]) > 0:
        attr = path[0]

    return RuleSpec(scope, template, rule_id, attr)


def _rule_tuple_from_request_object(
    rule_template: str, rule_id: str, req_obj: JsonDict
) -> Tuple[List[JsonDict], List[Union[str, JsonDict]]]:
    if rule_template in ["override", "underride"]:
        if "conditions" not in req_obj:
            raise InvalidRuleException("Missing 'conditions'")
        conditions = req_obj["conditions"]
        for c in conditions:
            if "kind" not in c:
                raise InvalidRuleException("Condition without 'kind'")
    elif rule_template == "room":
        conditions = [{"kind": "event_match", "key": "room_id", "pattern": rule_id}]
    elif rule_template == "sender":
        conditions = [{"kind": "event_match", "key": "user_id", "pattern": rule_id}]
    elif rule_template == "content":
        if "pattern" not in req_obj:
            raise InvalidRuleException("Content rule missing 'pattern'")
        pat = req_obj["pattern"]

        conditions = [{"kind": "event_match", "key": "content.body", "pattern": pat}]
    else:
        raise InvalidRuleException("Unknown rule template: %s" % (rule_template,))

    if "actions" not in req_obj:
        raise InvalidRuleException("No actions found")
    actions = req_obj["actions"]

    check_actions(actions)

    return conditions, actions


def _filter_ruleset_with_path(ruleset: JsonDict, path: List[str]) -> JsonDict:
    if path == []:
        raise UnrecognizedRequestError(
            PushRuleRestServlet.SLIGHTLY_PEDANTIC_TRAILING_SLASH_ERROR
        )

    if path[0] == "":
        return ruleset
    template_kind = path[0]
    if template_kind not in ruleset:
        raise UnrecognizedRequestError()
    path = path[1:]
    if path == []:
        raise UnrecognizedRequestError(
            PushRuleRestServlet.SLIGHTLY_PEDANTIC_TRAILING_SLASH_ERROR
        )
    if path[0] == "":
        return ruleset[template_kind]
    rule_id = path[0]

    the_rule = None
    for r in ruleset[template_kind]:
        if r["rule_id"] == rule_id:
            the_rule = r
    if the_rule is None:
        raise NotFoundError()

    path = path[1:]
    if len(path) == 0:
        return the_rule

    attr = path[0]
    if attr in the_rule:
        # Make sure we return a JSON object as the attribute may be a
        # JSON value.
        return {attr: the_rule[attr]}
    else:
        raise UnrecognizedRequestError()


def _priority_class_from_spec(spec: RuleSpec) -> int:
    if spec.template not in PRIORITY_CLASS_MAP.keys():
        raise InvalidRuleException("Unknown template: %s" % (spec.template))
    pc = PRIORITY_CLASS_MAP[spec.template]

    return pc


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    PushRuleRestServlet(hs).register(http_server)
