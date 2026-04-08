#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
from http import HTTPStatus

import synapse
from synapse.api.errors import Codes
from synapse.rest.client import login, push_rule, room

from tests.unittest import HomeserverTestCase


class PushRuleAttributesTestCase(HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
        push_rule.register_servlets,
    ]
    hijack_auth = False

    def test_enabled_on_creation(self) -> None:
        """
        Tests the GET and PUT of push rules' `enabled` endpoints.
        Tests that a rule is enabled upon creation, even though a rule with that
            ruleId existed previously and was disabled.
        """
        self.register_user("user", "pass")
        token = self.login("user", "pass")

        body = {
            "conditions": [
                {"kind": "event_match", "key": "sender", "pattern": "@user2:hs"}
            ],
            "actions": ["notify", {"set_tweak": "highlight"}],
        }

        # PUT a new rule
        channel = self.make_request(
            "PUT", "/pushrules/global/override/best.friend", body, access_token=token
        )
        self.assertEqual(channel.code, 200)

        # GET enabled for that new rule
        channel = self.make_request(
            "GET", "/pushrules/global/override/best.friend/enabled", access_token=token
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["enabled"], True)

    def test_enabled_on_recreation(self) -> None:
        """
        Tests the GET and PUT of push rules' `enabled` endpoints.
        Tests that a rule is enabled upon creation, even if a rule with that
            ruleId existed previously and was disabled.
        """
        self.register_user("user", "pass")
        token = self.login("user", "pass")

        body = {
            "conditions": [
                {"kind": "event_match", "key": "sender", "pattern": "@user2:hs"}
            ],
            "actions": ["notify", {"set_tweak": "highlight"}],
        }

        # PUT a new rule
        channel = self.make_request(
            "PUT", "/pushrules/global/override/best.friend", body, access_token=token
        )
        self.assertEqual(channel.code, 200)

        # disable the rule
        channel = self.make_request(
            "PUT",
            "/pushrules/global/override/best.friend/enabled",
            {"enabled": False},
            access_token=token,
        )
        self.assertEqual(channel.code, 200)

        # check rule disabled
        channel = self.make_request(
            "GET", "/pushrules/global/override/best.friend/enabled", access_token=token
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["enabled"], False)

        # DELETE the rule
        channel = self.make_request(
            "DELETE", "/pushrules/global/override/best.friend", access_token=token
        )
        self.assertEqual(channel.code, 200)

        # PUT a new rule
        channel = self.make_request(
            "PUT", "/pushrules/global/override/best.friend", body, access_token=token
        )
        self.assertEqual(channel.code, 200)

        # GET enabled for that new rule
        channel = self.make_request(
            "GET", "/pushrules/global/override/best.friend/enabled", access_token=token
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["enabled"], True)

    def test_enabled_disable(self) -> None:
        """
        Tests the GET and PUT of push rules' `enabled` endpoints.
        Tests that a rule is disabled and enabled when we ask for it.
        """
        self.register_user("user", "pass")
        token = self.login("user", "pass")

        body = {
            "conditions": [
                {"kind": "event_match", "key": "sender", "pattern": "@user2:hs"}
            ],
            "actions": ["notify", {"set_tweak": "highlight"}],
        }

        # PUT a new rule
        channel = self.make_request(
            "PUT", "/pushrules/global/override/best.friend", body, access_token=token
        )
        self.assertEqual(channel.code, 200)

        # disable the rule
        channel = self.make_request(
            "PUT",
            "/pushrules/global/override/best.friend/enabled",
            {"enabled": False},
            access_token=token,
        )
        self.assertEqual(channel.code, 200)

        # check rule disabled
        channel = self.make_request(
            "GET", "/pushrules/global/override/best.friend/enabled", access_token=token
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["enabled"], False)

        # re-enable the rule
        channel = self.make_request(
            "PUT",
            "/pushrules/global/override/best.friend/enabled",
            {"enabled": True},
            access_token=token,
        )
        self.assertEqual(channel.code, 200)

        # check rule enabled
        channel = self.make_request(
            "GET", "/pushrules/global/override/best.friend/enabled", access_token=token
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["enabled"], True)

    def test_enabled_404_when_get_non_existent(self) -> None:
        """
        Tests that `enabled` gives 404 when the rule doesn't exist.
        """
        self.register_user("user", "pass")
        token = self.login("user", "pass")

        body = {
            "conditions": [
                {"kind": "event_match", "key": "sender", "pattern": "@user2:hs"}
            ],
            "actions": ["notify", {"set_tweak": "highlight"}],
        }

        # check 404 for never-heard-of rule
        channel = self.make_request(
            "GET", "/pushrules/global/override/best.friend/enabled", access_token=token
        )
        self.assertEqual(channel.code, 404)
        self.assertEqual(channel.json_body["errcode"], Codes.NOT_FOUND)

        # PUT a new rule
        channel = self.make_request(
            "PUT", "/pushrules/global/override/best.friend", body, access_token=token
        )
        self.assertEqual(channel.code, 200)

        # GET enabled for that new rule
        channel = self.make_request(
            "GET", "/pushrules/global/override/best.friend/enabled", access_token=token
        )
        self.assertEqual(channel.code, 200)

        # DELETE the rule
        channel = self.make_request(
            "DELETE", "/pushrules/global/override/best.friend", access_token=token
        )
        self.assertEqual(channel.code, 200)

        # check 404 for deleted rule
        channel = self.make_request(
            "GET", "/pushrules/global/override/best.friend/enabled", access_token=token
        )
        self.assertEqual(channel.code, 404)
        self.assertEqual(channel.json_body["errcode"], Codes.NOT_FOUND)

    def test_enabled_404_when_get_non_existent_server_rule(self) -> None:
        """
        Tests that `enabled` gives 404 when the server-default rule doesn't exist.
        """
        self.register_user("user", "pass")
        token = self.login("user", "pass")

        # check 404 for never-heard-of rule
        channel = self.make_request(
            "GET", "/pushrules/global/override/.m.muahahaha/enabled", access_token=token
        )
        self.assertEqual(channel.code, 404)
        self.assertEqual(channel.json_body["errcode"], Codes.NOT_FOUND)

    def test_enabled_404_when_put_non_existent_rule(self) -> None:
        """
        Tests that `enabled` gives 404 when we put to a rule that doesn't exist.
        """
        self.register_user("user", "pass")
        token = self.login("user", "pass")

        # enable & check 404 for never-heard-of rule
        channel = self.make_request(
            "PUT",
            "/pushrules/global/override/best.friend/enabled",
            {"enabled": True},
            access_token=token,
        )
        self.assertEqual(channel.code, 404)
        self.assertEqual(channel.json_body["errcode"], Codes.NOT_FOUND)

    def test_enabled_404_when_put_non_existent_server_rule(self) -> None:
        """
        Tests that `enabled` gives 404 when we put to a server-default rule that doesn't exist.
        """
        self.register_user("user", "pass")
        token = self.login("user", "pass")

        # enable & check 404 for never-heard-of rule
        channel = self.make_request(
            "PUT",
            "/pushrules/global/override/.m.muahahah/enabled",
            {"enabled": True},
            access_token=token,
        )
        self.assertEqual(channel.code, 404)
        self.assertEqual(channel.json_body["errcode"], Codes.NOT_FOUND)

    def test_actions_get(self) -> None:
        """
        Tests that `actions` gives you what you expect on a fresh rule.
        """
        self.register_user("user", "pass")
        token = self.login("user", "pass")

        body = {
            "conditions": [
                {"kind": "event_match", "key": "sender", "pattern": "@user2:hs"}
            ],
            "actions": ["notify", {"set_tweak": "highlight"}],
        }

        # PUT a new rule
        channel = self.make_request(
            "PUT", "/pushrules/global/override/best.friend", body, access_token=token
        )
        self.assertEqual(channel.code, 200)

        # GET actions for that new rule
        channel = self.make_request(
            "GET", "/pushrules/global/override/best.friend/actions", access_token=token
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body["actions"], ["notify", {"set_tweak": "highlight"}]
        )

    def test_actions_put(self) -> None:
        """
        Tests that PUT on actions updates the value you'd get from GET.
        """
        self.register_user("user", "pass")
        token = self.login("user", "pass")

        body = {
            "conditions": [
                {"kind": "event_match", "key": "sender", "pattern": "@user2:hs"}
            ],
            "actions": ["notify", {"set_tweak": "highlight"}],
        }

        # PUT a new rule
        channel = self.make_request(
            "PUT", "/pushrules/global/override/best.friend", body, access_token=token
        )
        self.assertEqual(channel.code, 200)

        # change the rule actions
        channel = self.make_request(
            "PUT",
            "/pushrules/global/override/best.friend/actions",
            {"actions": ["dont_notify"]},
            access_token=token,
        )
        self.assertEqual(channel.code, 200)

        # GET actions for that new rule
        channel = self.make_request(
            "GET", "/pushrules/global/override/best.friend/actions", access_token=token
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["actions"], ["dont_notify"])

    def test_actions_404_when_get_non_existent(self) -> None:
        """
        Tests that `actions` gives 404 when the rule doesn't exist.
        """
        self.register_user("user", "pass")
        token = self.login("user", "pass")

        body = {
            "conditions": [
                {"kind": "event_match", "key": "sender", "pattern": "@user2:hs"}
            ],
            "actions": ["notify", {"set_tweak": "highlight"}],
        }

        # check 404 for never-heard-of rule
        channel = self.make_request(
            "GET", "/pushrules/global/override/best.friend/enabled", access_token=token
        )
        self.assertEqual(channel.code, 404)
        self.assertEqual(channel.json_body["errcode"], Codes.NOT_FOUND)

        # PUT a new rule
        channel = self.make_request(
            "PUT", "/pushrules/global/override/best.friend", body, access_token=token
        )
        self.assertEqual(channel.code, 200)

        # DELETE the rule
        channel = self.make_request(
            "DELETE", "/pushrules/global/override/best.friend", access_token=token
        )
        self.assertEqual(channel.code, 200)

        # check 404 for deleted rule
        channel = self.make_request(
            "GET", "/pushrules/global/override/best.friend/enabled", access_token=token
        )
        self.assertEqual(channel.code, 404)
        self.assertEqual(channel.json_body["errcode"], Codes.NOT_FOUND)

    def test_actions_404_when_get_non_existent_server_rule(self) -> None:
        """
        Tests that `actions` gives 404 when the server-default rule doesn't exist.
        """
        self.register_user("user", "pass")
        token = self.login("user", "pass")

        # check 404 for never-heard-of rule
        channel = self.make_request(
            "GET", "/pushrules/global/override/.m.muahahaha/actions", access_token=token
        )
        self.assertEqual(channel.code, 404)
        self.assertEqual(channel.json_body["errcode"], Codes.NOT_FOUND)

    def test_actions_404_when_put_non_existent_rule(self) -> None:
        """
        Tests that `actions` gives 404 when putting to a rule that doesn't exist.
        """
        self.register_user("user", "pass")
        token = self.login("user", "pass")

        # enable & check 404 for never-heard-of rule
        channel = self.make_request(
            "PUT",
            "/pushrules/global/override/best.friend/actions",
            {"actions": ["dont_notify"]},
            access_token=token,
        )
        self.assertEqual(channel.code, 404)
        self.assertEqual(channel.json_body["errcode"], Codes.NOT_FOUND)

    def test_actions_404_when_put_non_existent_server_rule(self) -> None:
        """
        Tests that `actions` gives 404 when putting to a server-default rule that doesn't exist.
        """
        self.register_user("user", "pass")
        token = self.login("user", "pass")

        # enable & check 404 for never-heard-of rule
        channel = self.make_request(
            "PUT",
            "/pushrules/global/override/.m.muahahah/actions",
            {"actions": ["dont_notify"]},
            access_token=token,
        )
        self.assertEqual(channel.code, 404)
        self.assertEqual(channel.json_body["errcode"], Codes.NOT_FOUND)

    def test_contains_user_name(self) -> None:
        """
        Tests that `contains_user_name` rule is present and have proper value in `pattern`.
        """
        username = "bob"
        self.register_user(username, "pass")
        token = self.login(username, "pass")

        channel = self.make_request(
            "GET",
            "/pushrules/global/content/.m.rule.contains_user_name",
            access_token=token,
        )

        self.assertEqual(channel.code, 200)

        self.assertEqual(
            {
                "rule_id": ".m.rule.contains_user_name",
                "default": True,
                "enabled": True,
                "pattern": username,
                "actions": [
                    "notify",
                    {"set_tweak": "highlight"},
                    {"set_tweak": "sound", "value": "default"},
                ],
            },
            channel.json_body,
        )

    def test_is_user_mention(self) -> None:
        """
        Tests that `is_user_mention` rule is present and have proper value in `value`.
        """
        user = self.register_user("bob", "pass")
        token = self.login("bob", "pass")

        channel = self.make_request(
            "GET",
            "/pushrules/global/override/.m.rule.is_user_mention",
            access_token=token,
        )

        self.assertEqual(channel.code, 200)

        self.assertEqual(
            {
                "rule_id": ".m.rule.is_user_mention",
                "default": True,
                "enabled": True,
                "conditions": [
                    {
                        "kind": "event_property_contains",
                        "key": "content.m\\.mentions.user_ids",
                        "value": user,
                    }
                ],
                "actions": [
                    "notify",
                    {"set_tweak": "highlight"},
                    {"set_tweak": "sound", "value": "default"},
                ],
            },
            channel.json_body,
        )

    def test_no_user_defined_postcontent_rules(self) -> None:
        """
        Tests that clients are not permitted to create MSC4306 `postcontent` rules.
        """
        self.register_user("bob", "pass")
        token = self.login("bob", "pass")

        channel = self.make_request(
            "PUT",
            "/pushrules/global/postcontent/some.user.rule",
            {},
            access_token=token,
        )

        self.assertEqual(channel.code, HTTPStatus.BAD_REQUEST)
        self.assertEqual(
            Codes.INVALID_PARAM,
            channel.json_body["errcode"],
        )
