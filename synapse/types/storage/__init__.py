#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
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


class _BackgroundUpdates:
    EVENT_ORIGIN_SERVER_TS_NAME = "event_origin_server_ts"
    EVENT_FIELDS_SENDER_URL_UPDATE_NAME = "event_fields_sender_url"
    DELETE_SOFT_FAILED_EXTREMITIES = "delete_soft_failed_extremities"
    POPULATE_STREAM_ORDERING2 = "populate_stream_ordering2"
    INDEX_STREAM_ORDERING2 = "index_stream_ordering2"
    INDEX_STREAM_ORDERING2_CONTAINS_URL = "index_stream_ordering2_contains_url"
    INDEX_STREAM_ORDERING2_ROOM_ORDER = "index_stream_ordering2_room_order"
    INDEX_STREAM_ORDERING2_ROOM_STREAM = "index_stream_ordering2_room_stream"
    INDEX_STREAM_ORDERING2_TS = "index_stream_ordering2_ts"
    REPLACE_STREAM_ORDERING_COLUMN = "replace_stream_ordering_column"

    EVENT_EDGES_DROP_INVALID_ROWS = "event_edges_drop_invalid_rows"
    EVENT_EDGES_REPLACE_INDEX = "event_edges_replace_index"

    EVENTS_POPULATE_STATE_KEY_REJECTIONS = "events_populate_state_key_rejections"

    EVENTS_JUMP_TO_DATE_INDEX = "events_jump_to_date_index"

    CURRENT_STATE_EVENTS_STREAM_ORDERING_INDEX_UPDATE_NAME = (
        "current_state_events_stream_ordering_idx"
    )
    ROOM_MEMBERSHIPS_STREAM_ORDERING_INDEX_UPDATE_NAME = (
        "room_memberships_stream_ordering_idx"
    )
    LOCAL_CURRENT_MEMBERSHIP_STREAM_ORDERING_INDEX_UPDATE_NAME = (
        "local_current_membership_stream_ordering_idx"
    )

    SLIDING_SYNC_PREFILL_JOINED_ROOMS_TO_RECALCULATE_TABLE_BG_UPDATE = (
        "sliding_sync_prefill_joined_rooms_to_recalculate_table_bg_update"
    )
    SLIDING_SYNC_JOINED_ROOMS_BG_UPDATE = "sliding_sync_joined_rooms_bg_update"
    SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BG_UPDATE = (
        "sliding_sync_membership_snapshots_bg_update"
    )
    SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_FIX_FORGOTTEN_COLUMN_BG_UPDATE = (
        "sliding_sync_membership_snapshots_fix_forgotten_column_bg_update"
    )

    MARK_UNREFERENCED_STATE_GROUPS_FOR_DELETION_BG_UPDATE = (
        "mark_unreferenced_state_groups_for_deletion_bg_update"
    )

    FIXUP_MAX_DEPTH_CAP = "fixup_max_depth_cap"
