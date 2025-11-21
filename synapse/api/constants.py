#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
# Copyright 2017 Vector Creations Ltd
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

"""Contains constants from the specification."""

import enum
from typing import Final

# the max size of a (canonical-json-encoded) event
MAX_PDU_SIZE = 65536

# Max/min size of ints in canonical JSON
CANONICALJSON_MAX_INT = (2**53) - 1
CANONICALJSON_MIN_INT = -CANONICALJSON_MAX_INT

# the "depth" field on events is limited to the same as what
# canonicaljson accepts
MAX_DEPTH = CANONICALJSON_MAX_INT

# the maximum length for a room alias is 255 characters
MAX_ALIAS_LENGTH = 255

# the maximum length for a user id is 255 characters
MAX_USERID_LENGTH = 255

# Constant value used for the pseudo-thread which is the main timeline.
MAIN_TIMELINE: Final = "main"

# MAX_INT + 1, so it always trumps any PL in canonical JSON.
CREATOR_POWER_LEVEL = 2**53


class Membership:
    """Represents the membership states of a user in a room."""

    INVITE: Final = "invite"
    JOIN: Final = "join"
    KNOCK: Final = "knock"
    LEAVE: Final = "leave"
    BAN: Final = "ban"
    LIST: Final = frozenset((INVITE, JOIN, KNOCK, LEAVE, BAN))


class PresenceState:
    """Represents the presence state of a user."""

    OFFLINE: Final = "offline"
    UNAVAILABLE: Final = "unavailable"
    ONLINE: Final = "online"
    BUSY: Final = "org.matrix.msc3026.busy"


class JoinRules:
    PUBLIC: Final = "public"
    KNOCK: Final = "knock"
    INVITE: Final = "invite"
    PRIVATE: Final = "private"
    # As defined for MSC3083.
    RESTRICTED: Final = "restricted"
    # As defined for MSC3787.
    KNOCK_RESTRICTED: Final = "knock_restricted"


class RestrictedJoinRuleTypes:
    """Understood types for the allow rules in restricted join rules."""

    ROOM_MEMBERSHIP: Final = "m.room_membership"


class LoginType:
    PASSWORD: Final = "m.login.password"
    EMAIL_IDENTITY: Final = "m.login.email.identity"
    MSISDN: Final = "m.login.msisdn"
    RECAPTCHA: Final = "m.login.recaptcha"
    TERMS: Final = "m.login.terms"
    SSO: Final = "m.login.sso"
    DUMMY: Final = "m.login.dummy"
    REGISTRATION_TOKEN: Final = "m.login.registration_token"


# This is used in the `type` parameter for /register when called by
# an appservice to register a new user.
APP_SERVICE_REGISTRATION_TYPE: Final = "m.login.application_service"


class EventTypes:
    Member: Final = "m.room.member"
    Create: Final = "m.room.create"
    Tombstone: Final = "m.room.tombstone"
    JoinRules: Final = "m.room.join_rules"
    PowerLevels: Final = "m.room.power_levels"
    Aliases: Final = "m.room.aliases"
    Redaction: Final = "m.room.redaction"
    ThirdPartyInvite: Final = "m.room.third_party_invite"

    RoomHistoryVisibility: Final = "m.room.history_visibility"
    CanonicalAlias: Final = "m.room.canonical_alias"
    Encrypted: Final = "m.room.encrypted"
    RoomAvatar: Final = "m.room.avatar"
    RoomEncryption: Final = "m.room.encryption"
    GuestAccess: Final = "m.room.guest_access"

    # These are used for validation
    Message: Final = "m.room.message"
    Topic: Final = "m.room.topic"
    Name: Final = "m.room.name"

    ServerACL: Final = "m.room.server_acl"
    Pinned: Final = "m.room.pinned_events"

    Retention: Final = "m.room.retention"

    Dummy: Final = "org.matrix.dummy_event"

    SpaceChild: Final = "m.space.child"
    SpaceParent: Final = "m.space.parent"

    Reaction: Final = "m.reaction"
    Sticker: Final = "m.sticker"
    LiveLocationShareStart: Final = "m.beacon_info"

    CallInvite: Final = "m.call.invite"

    PollStart: Final = "m.poll.start"


class ToDeviceEventTypes:
    RoomKeyRequest: Final = "m.room_key_request"


class DeviceKeyAlgorithms:
    """Spec'd algorithms for the generation of per-device keys"""

    ED25519: Final = "ed25519"
    CURVE25519: Final = "curve25519"
    SIGNED_CURVE25519: Final = "signed_curve25519"


class EduTypes:
    PRESENCE: Final = "m.presence"
    TYPING: Final = "m.typing"
    RECEIPT: Final = "m.receipt"
    DEVICE_LIST_UPDATE: Final = "m.device_list_update"
    SIGNING_KEY_UPDATE: Final = "m.signing_key_update"
    UNSTABLE_SIGNING_KEY_UPDATE: Final = "org.matrix.signing_key_update"
    DIRECT_TO_DEVICE: Final = "m.direct_to_device"


class RejectedReason:
    AUTH_ERROR: Final = "auth_error"
    OVERSIZED_EVENT: Final = "oversized_event"


class RoomCreationPreset:
    PRIVATE_CHAT: Final = "private_chat"
    PUBLIC_CHAT: Final = "public_chat"
    TRUSTED_PRIVATE_CHAT: Final = "trusted_private_chat"


class ThirdPartyEntityKind:
    USER: Final = "user"
    LOCATION: Final = "location"


ServerNoticeMsgType: Final = "m.server_notice"
ServerNoticeLimitReached: Final = "m.server_notice.usage_limit_reached"


class UserTypes:
    """Allows for user type specific behaviour. With the benefit of hindsight
    'admin' and 'guest' users should also be UserTypes. Extra user types can be
    added in the configuration. Normal users are type None or one of the extra
    user types (if configured).
    """

    SUPPORT: Final = "support"
    BOT: Final = "bot"
    ALL_BUILTIN_USER_TYPES: Final = (SUPPORT, BOT)
    """
    The user types that are built-in to Synapse. Extra user types can be
    added in the configuration.
    """


class RelationTypes:
    """The types of relations known to this server."""

    ANNOTATION: Final = "m.annotation"
    REPLACE: Final = "m.replace"
    REFERENCE: Final = "m.reference"
    THREAD: Final = "m.thread"


class LimitBlockingTypes:
    """Reasons that a server may be blocked"""

    MONTHLY_ACTIVE_USER: Final = "monthly_active_user"
    HS_DISABLED: Final = "hs_disabled"


class EventContentFields:
    """Fields found in events' content, regardless of type."""

    # Labels for the event, cf https://github.com/matrix-org/matrix-doc/pull/2326
    LABELS: Final = "org.matrix.labels"

    # Timestamp to delete the event after
    # cf https://github.com/matrix-org/matrix-doc/pull/2228
    SELF_DESTRUCT_AFTER: Final = "org.matrix.self_destruct_after"

    # cf https://github.com/matrix-org/matrix-doc/pull/1772
    ROOM_TYPE: Final = "type"

    # Whether a room can federate.
    FEDERATE: Final = "m.federate"

    # The creator of the room, as used in `m.room.create` events.
    #
    # This is deprecated in MSC2175.
    ROOM_CREATOR: Final = "creator"
    # MSC4289
    ADDITIONAL_CREATORS: Final = "additional_creators"

    # The version of the room for `m.room.create` events.
    ROOM_VERSION: Final = "room_version"

    ROOM_NAME: Final = "name"

    MEMBERSHIP: Final = "membership"
    MEMBERSHIP_DISPLAYNAME: Final = "displayname"
    MEMBERSHIP_AVATAR_URL: Final = "avatar_url"

    # Used in m.room.guest_access events.
    GUEST_ACCESS: Final = "guest_access"

    # The authorising user for joining a restricted room.
    AUTHORISING_USER: Final = "join_authorised_via_users_server"

    # Use for mentioning users.
    MENTIONS: Final = "m.mentions"

    # an unspecced field added to to-device messages to identify them uniquely-ish
    TO_DEVICE_MSGID: Final = "org.matrix.msgid"

    # `m.room.encryption`` algorithm field
    ENCRYPTION_ALGORITHM: Final = "algorithm"

    TOMBSTONE_SUCCESSOR_ROOM: Final = "replacement_room"

    # Used in m.room.topic events.
    TOPIC: Final = "topic"
    M_TOPIC: Final = "m.topic"
    M_TEXT: Final = "m.text"


class EventUnsignedContentFields:
    """Fields found inside the 'unsigned' data on events"""

    # Requesting user's membership, per MSC4115
    MEMBERSHIP: Final = "membership"


class MTextFields:
    """Fields found inside m.text content blocks."""

    BODY: Final = "body"
    MIMETYPE: Final = "mimetype"


class RoomTypes:
    """Understood values of the room_type field of m.room.create events."""

    SPACE: Final = "m.space"


class RoomEncryptionAlgorithms:
    MEGOLM_V1_AES_SHA2: Final = "m.megolm.v1.aes-sha2"
    DEFAULT: Final = MEGOLM_V1_AES_SHA2


class AccountDataTypes:
    DIRECT: Final = "m.direct"
    IGNORED_USER_LIST: Final = "m.ignored_user_list"
    TAG: Final = "m.tag"
    PUSH_RULES: Final = "m.push_rules"
    # MSC4155: Invite filtering
    MSC4155_INVITE_PERMISSION_CONFIG: Final = (
        "org.matrix.msc4155.invite_permission_config"
    )
    # Synapse-specific behaviour. See "Client-Server API Extensions" documentation
    # in Admin API for more information.
    SYNAPSE_ADMIN_CLIENT_CONFIG: Final = "io.element.synapse.admin_client_config"


class HistoryVisibility:
    INVITED: Final = "invited"
    JOINED: Final = "joined"
    SHARED: Final = "shared"
    WORLD_READABLE: Final = "world_readable"


class GuestAccess:
    CAN_JOIN: Final = "can_join"
    # anything that is not "can_join" is considered "forbidden", but for completeness:
    FORBIDDEN: Final = "forbidden"


class ReceiptTypes:
    READ: Final = "m.read"
    READ_PRIVATE: Final = "m.read.private"
    FULLY_READ: Final = "m.fully_read"


class PublicRoomsFilterFields:
    """Fields in the search filter for `/publicRooms` that we understand.

    As defined in https://spec.matrix.org/v1.3/client-server-api/#post_matrixclientv3publicrooms
    """

    GENERIC_SEARCH_TERM: Final = "generic_search_term"
    ROOM_TYPES: Final = "room_types"


class ApprovalNoticeMedium:
    """Identifier for the medium this server will use to serve notice of approval for a
    specific user's registration.

    As defined in https://github.com/matrix-org/matrix-spec-proposals/blob/babolivier/m_not_approved/proposals/3866-user-not-approved-error.md
    """

    NONE = "org.matrix.msc3866.none"
    EMAIL = "org.matrix.msc3866.email"


class Direction(enum.Enum):
    BACKWARDS = "b"
    FORWARDS = "f"


class ProfileFields:
    DISPLAYNAME: Final = "displayname"
    AVATAR_URL: Final = "avatar_url"
