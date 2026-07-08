#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
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
import re
from typing import Any, Generator
from unittest.mock import AsyncMock, Mock

from twisted.internet import defer

from synapse.appservice import ApplicationService, Namespace
from synapse.types import UserID

from tests import unittest


def _regex(regex: str, exclusive: bool = True) -> Namespace:
    return Namespace(exclusive, re.compile(regex))


class ApplicationServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ApplicationService(
            id="unique_identifier",
            sender=UserID.from_string("@as:test"),
            url="some_url",
            token="some_token",
        )
        self.event = Mock(
            event_id="$abc:xyz",
            type="m.something",
            room_id="!foo:bar",
            sender="@someone:somewhere",
        )

        self.store = Mock()
        self.store.get_aliases_for_room = AsyncMock(return_value=[])
        self.store.get_local_users_in_room = AsyncMock(return_value=[])

    @defer.inlineCallbacks
    def test_regex_user_id_prefix_match(
        self,
    ) -> Generator["defer.Deferred[Any]", object, None]:
        self.service.namespaces[ApplicationService.NS_USERS].append(_regex("@irc_.*"))
        self.event.sender = "@irc_foobar:matrix.org"
        self.assertTrue(
            (
                yield self.service.is_interested_in_event(
                    self.event.event_id, self.event, self.store
                )
            )
        )

    @defer.inlineCallbacks
    def test_regex_user_id_prefix_no_match(
        self,
    ) -> Generator["defer.Deferred[Any]", object, None]:
        self.service.namespaces[ApplicationService.NS_USERS].append(_regex("@irc_.*"))
        self.event.sender = "@someone_else:matrix.org"
        self.assertFalse(
            (
                yield self.service.is_interested_in_event(
                    self.event.event_id, self.event, self.store
                )
            )
        )

    @defer.inlineCallbacks
    def test_regex_room_member_is_checked(
        self,
    ) -> Generator["defer.Deferred[Any]", object, None]:
        self.service.namespaces[ApplicationService.NS_USERS].append(_regex("@irc_.*"))
        self.event.sender = "@someone_else:matrix.org"
        self.event.type = "m.room.member"
        self.event.state_key = "@irc_foobar:matrix.org"
        self.assertTrue(
            (
                yield self.service.is_interested_in_event(
                    self.event.event_id, self.event, self.store
                )
            )
        )

    @defer.inlineCallbacks
    def test_regex_room_id_match(
        self,
    ) -> Generator["defer.Deferred[Any]", object, None]:
        self.service.namespaces[ApplicationService.NS_ROOMS].append(
            _regex("!some_prefix.*some_suffix:matrix.org")
        )
        self.event.room_id = "!some_prefixs0m3th1nGsome_suffix:matrix.org"
        self.assertTrue(
            (
                yield self.service.is_interested_in_event(
                    self.event.event_id, self.event, self.store
                )
            )
        )

    @defer.inlineCallbacks
    def test_regex_room_id_no_match(
        self,
    ) -> Generator["defer.Deferred[Any]", object, None]:
        self.service.namespaces[ApplicationService.NS_ROOMS].append(
            _regex("!some_prefix.*some_suffix:matrix.org")
        )
        self.event.room_id = "!XqBunHwQIXUiqCaoxq:matrix.org"
        self.assertFalse(
            (
                yield self.service.is_interested_in_event(
                    self.event.event_id, self.event, self.store
                )
            )
        )

    @defer.inlineCallbacks
    def test_regex_alias_match(self) -> Generator["defer.Deferred[Any]", object, None]:
        self.service.namespaces[ApplicationService.NS_ALIASES].append(
            _regex("#irc_.*:matrix.org")
        )
        self.store.get_aliases_for_room = AsyncMock(
            return_value=["#irc_foobar:matrix.org", "#athing:matrix.org"]
        )
        self.store.get_local_users_in_room = AsyncMock(return_value=[])
        self.assertTrue(
            (
                yield self.service.is_interested_in_event(
                    self.event.event_id, self.event, self.store
                )
            )
        )

    def test_non_exclusive_alias(self) -> None:
        self.service.namespaces[ApplicationService.NS_ALIASES].append(
            _regex("#irc_.*:matrix.org", exclusive=False)
        )
        self.assertFalse(self.service.is_exclusive_alias("#irc_foobar:matrix.org"))

    def test_non_exclusive_room(self) -> None:
        self.service.namespaces[ApplicationService.NS_ROOMS].append(
            _regex("!irc_.*:matrix.org", exclusive=False)
        )
        self.assertFalse(self.service.is_exclusive_room("!irc_foobar:matrix.org"))

    def test_non_exclusive_user(self) -> None:
        self.service.namespaces[ApplicationService.NS_USERS].append(
            _regex("@irc_.*:matrix.org", exclusive=False)
        )
        self.assertFalse(self.service.is_exclusive_user("@irc_foobar:matrix.org"))

    def test_exclusive_alias(self) -> None:
        self.service.namespaces[ApplicationService.NS_ALIASES].append(
            _regex("#irc_.*:matrix.org", exclusive=True)
        )
        self.assertTrue(self.service.is_exclusive_alias("#irc_foobar:matrix.org"))

    def test_exclusive_user(self) -> None:
        self.service.namespaces[ApplicationService.NS_USERS].append(
            _regex("@irc_.*:matrix.org", exclusive=True)
        )
        self.assertTrue(self.service.is_exclusive_user("@irc_foobar:matrix.org"))

    def test_exclusive_room(self) -> None:
        self.service.namespaces[ApplicationService.NS_ROOMS].append(
            _regex("!irc_.*:matrix.org", exclusive=True)
        )
        self.assertTrue(self.service.is_exclusive_room("!irc_foobar:matrix.org"))

    @defer.inlineCallbacks
    def test_regex_alias_no_match(
        self,
    ) -> Generator["defer.Deferred[Any]", object, None]:
        self.service.namespaces[ApplicationService.NS_ALIASES].append(
            _regex("#irc_.*:matrix.org")
        )
        self.store.get_aliases_for_room = AsyncMock(
            return_value=["#xmpp_foobar:matrix.org", "#athing:matrix.org"]
        )
        self.store.get_local_users_in_room = AsyncMock(return_value=[])
        self.assertFalse(
            (
                yield defer.ensureDeferred(
                    self.service.is_interested_in_event(
                        self.event.event_id, self.event, self.store
                    )
                )
            )
        )

    @defer.inlineCallbacks
    def test_regex_multiple_matches(
        self,
    ) -> Generator["defer.Deferred[Any]", object, None]:
        self.service.namespaces[ApplicationService.NS_ALIASES].append(
            _regex("#irc_.*:matrix.org")
        )
        self.service.namespaces[ApplicationService.NS_USERS].append(_regex("@irc_.*"))
        self.event.sender = "@irc_foobar:matrix.org"
        self.store.get_aliases_for_room = AsyncMock(
            return_value=["#irc_barfoo:matrix.org"]
        )
        self.store.get_local_users_in_room = AsyncMock(return_value=[])
        self.assertTrue(
            (
                yield self.service.is_interested_in_event(
                    self.event.event_id, self.event, self.store
                )
            )
        )

    @defer.inlineCallbacks
    def test_interested_in_self(self) -> Generator["defer.Deferred[Any]", object, None]:
        # make sure invites get through
        self.service.sender = UserID.from_string("@appservice:name")
        self.service.namespaces[ApplicationService.NS_USERS].append(_regex("@irc_.*"))
        self.event.type = "m.room.member"
        self.event.content = {"membership": "invite"}
        self.event.state_key = self.service.sender.to_string()
        self.assertTrue(
            (
                yield self.service.is_interested_in_event(
                    self.event.event_id, self.event, self.store
                )
            )
        )

    @defer.inlineCallbacks
    def test_member_list_match(self) -> Generator["defer.Deferred[Any]", object, None]:
        self.service.namespaces[ApplicationService.NS_USERS].append(_regex("@irc_.*"))
        # Note that @irc_fo:here is the AS user.
        self.store.get_local_users_in_room = AsyncMock(
            return_value=["@alice:here", "@irc_fo:here", "@bob:here"]
        )
        self.store.get_aliases_for_room = AsyncMock(return_value=[])

        self.event.sender = "@xmpp_foobar:matrix.org"
        self.assertTrue(
            (
                yield self.service.is_interested_in_event(
                    self.event.event_id, self.event, self.store
                )
            )
        )
