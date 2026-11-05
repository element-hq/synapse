#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 Šimon Brandner <simon.bra.ag@gmail.com>
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

from copy import deepcopy
from unittest.mock import AsyncMock, patch

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EduTypes, Membership, ReceiptTypes
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.types import JsonDict, ReadReceipt
from synapse.util.clock import Clock

from tests import unittest


class ReceiptsTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.event_source = hs.get_event_sources().sources.receipt
        self.receipts_handler = hs.get_receipts_handler()

    def test_filters_out_private_receipt(self) -> None:
        self._test_filters_private(
            [
                {
                    "content": {
                        "$1435641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ_PRIVATE: {
                                "@rikj:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        }
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": EduTypes.RECEIPT,
                }
            ],
            [],
        )

    def test_filters_out_private_receipt_and_ignores_rest(self) -> None:
        self._test_filters_private(
            [
                {
                    "content": {
                        "$1dgdgrd5641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ_PRIVATE: {
                                "@rikj:jki.re": {
                                    "ts": 1436451550453,
                                },
                            },
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                },
                            },
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": EduTypes.RECEIPT,
                }
            ],
            [
                {
                    "content": {
                        "$1dgdgrd5641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        }
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": EduTypes.RECEIPT,
                }
            ],
        )

    def test_filters_out_event_with_only_private_receipts_and_ignores_the_rest(
        self,
    ) -> None:
        self._test_filters_private(
            [
                {
                    "content": {
                        "$14356419edgd14394fHBLK:matrix.org": {
                            ReceiptTypes.READ_PRIVATE: {
                                "@rikj:jki.re": {
                                    "ts": 1436451550453,
                                },
                            }
                        },
                        "$1435641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": EduTypes.RECEIPT,
                }
            ],
            [
                {
                    "content": {
                        "$1435641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        }
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": EduTypes.RECEIPT,
                }
            ],
        )

    def test_handles_empty_event(self) -> None:
        self._test_filters_private(
            [
                {
                    "content": {
                        "$143564gdfg6114394fHBLK:matrix.org": {},
                        "$1435641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": EduTypes.RECEIPT,
                }
            ],
            [
                {
                    "content": {
                        "$1435641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": EduTypes.RECEIPT,
                }
            ],
        )

    def test_filters_out_receipt_event_with_only_private_receipt_and_ignores_rest(
        self,
    ) -> None:
        self._test_filters_private(
            [
                {
                    "content": {
                        "$14356419edgd14394fHBLK:matrix.org": {
                            ReceiptTypes.READ_PRIVATE: {
                                "@rikj:jki.re": {
                                    "ts": 1436451550453,
                                },
                            }
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": EduTypes.RECEIPT,
                },
                {
                    "content": {
                        "$1435641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": EduTypes.RECEIPT,
                },
            ],
            [
                {
                    "content": {
                        "$1435641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        }
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": EduTypes.RECEIPT,
                }
            ],
        )

    def test_handles_string_data(self) -> None:
        """
        Tests that an invalid shape for read-receipts is handled.
        Context: https://github.com/matrix-org/synapse/issues/10603
        """

        self._test_filters_private(
            [
                {
                    "content": {
                        "$14356419edgd14394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@rikj:jki.re": "string",
                            }
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": EduTypes.RECEIPT,
                },
            ],
            [
                {
                    "content": {
                        "$14356419edgd14394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@rikj:jki.re": "string",
                            }
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": EduTypes.RECEIPT,
                },
            ],
        )

    def test_received_remote_receipt_ignores_malformed_entries(self) -> None:
        """
        A malformed `m.receipt` EDU from a remote server (e.g. a room's value,
        a receipt type's value, or a user's value being a string instead of an
        object) should not raise, and should not prevent other, well-formed
        receipts in the same EDU from being processed.

        Context: https://github.com/home-assistant/core/issues/153638 is an
        unrelated report, but https://github.com/matrix-org/synapse/issues/10376
        is the original report of this crash.
        """
        room_id = "!room:remote.example.org"
        good_user_id = "@good:remote.example.org"

        content: JsonDict = {
            # The whole room's value is a string, not an object.
            "!malformed-room-value:remote.example.org": "not a dict",
            room_id: {
                # The receipt type's value is a string, not an object.
                ReceiptTypes.READ: "not a dict",
                "a.receipt.type": {
                    good_user_id: {
                        "event_ids": ["$event:remote.example.org"],
                        "data": {"ts": 1436451550453},
                    },
                    # This user's value is a string, not an object.
                    "@malformed:remote.example.org": "not a dict",
                    # This user's event_ids list has non-string elements.
                    "@malformed-event-ids:remote.example.org": {
                        "event_ids": [1, None],
                    },
                },
            },
        }

        with patch.object(
            self.receipts_handler.event_auth_handler,
            "is_host_in_room",
            new=AsyncMock(return_value=True),
        ), patch.object(
            self.receipts_handler._storage_controllers.state,
            "get_current_hosts_in_room_or_partial_state_approximation",
            new=AsyncMock(return_value=["remote.example.org"]),
        ), patch.object(
            self.receipts_handler, "_handle_new_receipts", new=AsyncMock()
        ) as mock_handle_new_receipts:
            self.get_success(
                self.receipts_handler._received_remote_receipt(
                    "remote.example.org", content
                )
            )

        mock_handle_new_receipts.assert_called_once()
        (received_receipts,) = mock_handle_new_receipts.call_args.args
        self.assertEqual(
            received_receipts,
            [
                ReadReceipt(
                    room_id=room_id,
                    receipt_type="a.receipt.type",
                    user_id=good_user_id,
                    event_ids=["$event:remote.example.org"],
                    thread_id=None,
                    data={"ts": 1436451550453},
                )
            ],
        )

    def test_leaves_our_private_and_their_public(self) -> None:
        self._test_filters_private(
            [
                {
                    "content": {
                        "$1dgdgrd5641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ_PRIVATE: {
                                "@me:server.org": {
                                    "ts": 1436451550453,
                                },
                            },
                            ReceiptTypes.READ: {
                                "@rikj:jki.re": {
                                    "ts": 1436451550453,
                                },
                            },
                            "a.receipt.type": {
                                "@rikj:jki.re": {
                                    "ts": 1436451550453,
                                },
                            },
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": EduTypes.RECEIPT,
                }
            ],
            [
                {
                    "content": {
                        "$1dgdgrd5641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ_PRIVATE: {
                                "@me:server.org": {
                                    "ts": 1436451550453,
                                },
                            },
                            ReceiptTypes.READ: {
                                "@rikj:jki.re": {
                                    "ts": 1436451550453,
                                },
                            },
                            "a.receipt.type": {
                                "@rikj:jki.re": {
                                    "ts": 1436451550453,
                                },
                            },
                        }
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": EduTypes.RECEIPT,
                }
            ],
        )

    def test_we_do_not_mutate(self) -> None:
        """Ensure the input values are not modified."""
        events = [
            {
                "content": {
                    "$1435641916114394fHBLK:matrix.org": {
                        ReceiptTypes.READ_PRIVATE: {
                            "@rikj:jki.re": {
                                "ts": 1436451550453,
                            }
                        }
                    }
                },
                "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                "type": EduTypes.RECEIPT,
            }
        ]
        original_events = deepcopy(events)
        self._test_filters_private(events, [])
        # Since the events are fed in from a cache they should not be modified.
        self.assertEqual(events, original_events)

    def _test_filters_private(
        self, events: list[JsonDict], expected_output: list[JsonDict]
    ) -> None:
        """Tests that the _filter_out_private returns the expected output"""
        filtered_events = self.event_source.filter_out_private_receipts(
            events, "@me:server.org"
        )
        self.assertEqual(filtered_events, expected_output)


class FederationReceiptsTestCase(unittest.FederatingHomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        hs.get_receipts_handler()

    def test_non_compliant_user_id_receipt_dropped(self) -> None:
        """A remote server sends read receipts for a compliant and a
        non-compliant (grandfathered historical) user ID, both joined to a room
        we share. Only the compliant one is stored.
        """
        local_user_id = self.register_user("alice", "pass")
        local_user_token = self.login("alice", "pass")
        room_id = self.helper.create_room_as(local_user_id, tok=local_user_token)
        event_id = self.helper.send(room_id, tok=local_user_token)["event_id"]

        compliant_user_id = f"@john:{self.OTHER_SERVER_NAME}"
        non_compliant_user_id = f"@héllo:{self.OTHER_SERVER_NAME}"
        for user_id in (compliant_user_id, non_compliant_user_id):
            self.inject_room_member(room_id, user_id, Membership.JOIN)

        channel = self.make_signed_federation_request(
            "PUT",
            "/_matrix/federation/v1/send/txn",
            {
                "edus": [
                    {
                        "edu_type": EduTypes.RECEIPT,
                        "content": {
                            room_id: {
                                ReceiptTypes.READ: {
                                    user_id: {
                                        "event_ids": [event_id],
                                        "data": {"ts": 1436451550453},
                                    }
                                    for user_id in (
                                        compliant_user_id,
                                        non_compliant_user_id,
                                    )
                                }
                            }
                        },
                    }
                ]
            },
        )
        self.assertEqual(channel.code, 200, channel.result)

        store = self.hs.get_datastores().main
        self.assertEqual(
            self.get_success(
                store.get_receipts_for_user(compliant_user_id, [ReceiptTypes.READ])
            ),
            {room_id: event_id},
        )
        self.assertEqual(
            self.get_success(
                store.get_receipts_for_user(non_compliant_user_id, [ReceiptTypes.READ])
            ),
            {},
        )

    def test_malformed_receipt_edu_does_not_drop_wellformed_receipts(self) -> None:
        """A remote server sends a receipt EDU where one room's content is
        malformed (a string instead of an object, as seen in a real-world
        federation bug report: https://github.com/matrix-org/synapse/issues/10376).
        The malformed room should be ignored, but a well-formed receipt for
        another room in the same EDU should still be stored.
        """
        local_user_id = self.register_user("alice", "pass")
        local_user_token = self.login("alice", "pass")
        room_id = self.helper.create_room_as(local_user_id, tok=local_user_token)
        event_id = self.helper.send(room_id, tok=local_user_token)["event_id"]

        other_room_id = self.helper.create_room_as(local_user_id, tok=local_user_token)

        remote_user_id = f"@john:{self.OTHER_SERVER_NAME}"
        self.inject_room_member(room_id, remote_user_id, Membership.JOIN)
        self.inject_room_member(other_room_id, remote_user_id, Membership.JOIN)

        channel = self.make_signed_federation_request(
            "PUT",
            "/_matrix/federation/v1/send/txn",
            {
                "edus": [
                    {
                        "edu_type": EduTypes.RECEIPT,
                        "content": {
                            # Malformed: value should be an object keyed by
                            # receipt type, not a string.
                            other_room_id: "some string",
                            room_id: {
                                ReceiptTypes.READ: {
                                    remote_user_id: {
                                        "event_ids": [event_id],
                                        "data": {"ts": 1436451550453},
                                    }
                                }
                            },
                        },
                    }
                ]
            },
        )
        self.assertEqual(channel.code, 200, channel.result)

        store = self.hs.get_datastores().main
        self.assertEqual(
            self.get_success(
                store.get_receipts_for_user(remote_user_id, [ReceiptTypes.READ])
            ),
            {room_id: event_id},
        )
