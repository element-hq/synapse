# Server Announcements

These APIs read and send to the server announcement rooms configured under
`experimental_features.server_announcements.rooms` in `homeserver.yaml`. The
feature is experimental; see the
[configuration module](https://github.com/element-hq/synapse/blob/develop/synapse/config/server_announcements.py)
for how to configure the rooms.

Each room is identified by the `id` of its configuration entry, not by its
Matrix room ID.

You will need to authenticate with an access token for an admin user.

## List announcement rooms

```
GET /_synapse/admin/v1/server_announcements
```

Returns the configured announcement rooms:

```json
{
    "management_user_id": "@_server_announcements:example.com",
    "rooms": [
        {
            "id": "general",
            "room_id": "!abc:example.com",
            "room_name": "Server announcements",
            "room_topic": "Planned maintenance and incidents",
            "room_avatar_url": "mxc://example.com/xyz",
            "allowed_user_ids": ["@comms:example.com"],
            "excluded_user_ids": []
        }
    ]
}
```

* `management_user_id`: the user that creates and manages the rooms, and sends
  the announcements posted through this API.
* `rooms`: one entry per configured room, in configuration order. Empty if no
  rooms are configured.
  * `id`: the `id` of the configuration entry.
  * `room_id`: the Matrix room ID. `null` if the room has not been created,
    which happens after the room is purged while Synapse is running. Synapse
    creates it again on the next restart.
  * `room_name`, `room_topic`, `room_avatar_url`: the values from the
    configuration, which Synapse applies to the room at startup. Use the
    [room details API](rooms.md#room-details-api) for the room's current state.
  * `allowed_user_ids`, `excluded_user_ids`: as configured.

Rooms whose configuration entry was removed are not listed.

## Get an announcement room

```
GET /_synapse/admin/v1/server_announcements/{id}
```

Returns a single entry of `rooms` above, or a 404 with `M_NOT_FOUND` if `id` is
not in the configuration.
