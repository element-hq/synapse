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

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EduTypes, ReceiptTypes
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest


class ReceiptsTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.event_source = hs.get_event_sources().sources.receipt

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
