#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
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


from synapse.util.events import get_plain_text_topic_from_event_content

from tests import unittest


class EventsTestCase(unittest.TestCase):
    def test_get_plain_text_topic_no_topic(self) -> None:
        # No legacy or rich topic, expect None
        topic = get_plain_text_topic_from_event_content({})
        self.assertEqual(None, topic)

    def test_get_plain_text_topic_no_rich_topic(self) -> None:
        # Only legacy topic, expect legacy topic
        topic = get_plain_text_topic_from_event_content({"topic": "shenanigans"})
        self.assertEqual("shenanigans", topic)

    def test_get_plain_text_topic_rich_topic_without_representations(self) -> None:
        # Legacy topic and rich topic without representations, expect legacy topic
        topic = get_plain_text_topic_from_event_content(
            {"topic": "shenanigans", "m.topic": {"m.text": []}}
        )
        self.assertEqual("shenanigans", topic)

    def test_get_plain_text_topic_rich_topic_without_plain_text_representation(
        self,
    ) -> None:
        # Legacy topic and rich topic without plain text representation, expect legacy topic
        topic = get_plain_text_topic_from_event_content(
            {
                "topic": "shenanigans",
                "m.topic": {
                    "m.text": [
                        {"mimetype": "text/html", "body": "<strong>foobar</strong>"}
                    ]
                },
            }
        )
        self.assertEqual("shenanigans", topic)

    def test_get_plain_text_topic_rich_topic_with_plain_text_representation(
        self,
    ) -> None:
        # Legacy topic and rich topic with plain text representation, expect plain text representation
        topic = get_plain_text_topic_from_event_content(
            {
                "topic": "shenanigans",
                "m.topic": {"m.text": [{"mimetype": "text/plain", "body": "foobar"}]},
            }
        )
        self.assertEqual("foobar", topic)

    def test_get_plain_text_topic_rich_topic_with_implicit_plain_text_representation(
        self,
    ) -> None:
        # Legacy topic and rich topic with implicit plain text representation, expect plain text representation
        topic = get_plain_text_topic_from_event_content(
            {"topic": "shenanigans", "m.topic": {"m.text": [{"body": "foobar"}]}}
        )
        self.assertEqual("foobar", topic)

    def test_get_plain_text_topic_rich_topic_with_invalid_plain_text_representation(
        self,
    ) -> None:
        # Legacy topic and rich topic with invalid plain text representation, expect legacy topic
        topic = get_plain_text_topic_from_event_content(
            {"topic": "shenanigans", "m.topic": {"m.text": [{"body": 1337}]}}
        )
        self.assertEqual("shenanigans", topic)

    def test_get_plain_text_topic_rich_topic_with_invalid_and_second_valid_plain_text_representation(
        self,
    ) -> None:
        # Legacy topic and rich topic with invalid and second valid plain text representation, expect second plain text representation
        topic = get_plain_text_topic_from_event_content(
            {
                "topic": "shenanigans",
                "m.topic": {"m.text": [{"body": 1337}, {"body": "foobar"}]},
            }
        )
        self.assertEqual("foobar", topic)

    def test_get_plain_text_topic_rich_topic_with_plain_text_and_other_representation(
        self,
    ) -> None:
        # Legacy topic and rich topic with plain text representation, expect plain text representation
        topic = get_plain_text_topic_from_event_content(
            {
                "topic": "shenanigans",
                "m.topic": {
                    "m.text": [
                        {"mimetype": "text/html", "body": "<strong>foobar</strong>"},
                        {"mimetype": "text/plain", "body": "foobar"},
                    ]
                },
            }
        )
        self.assertEqual("foobar", topic)
