# Server Announcements

These APIs read and send to the server announcement rooms configured under
`experimental_features.server_announcements.rooms` in `homeserver.yaml`. The
feature is experimental; see the
[configuration module](https://github.com/element-hq/synapse/blob/develop/synapse/config/server_announcements.py)
for how to configure the rooms.

Each room is identified by the `id` of its configuration entry, not by its
Matrix room ID.

You will need to
[authenticate with an access token](https://element-hq.github.io/synapse/latest/usage/administration/admin_api/#authenticate-as-a-server-admin)
for an admin user.

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

## Send an announcement

```
POST /_synapse/admin/v1/server_announcements/{id}/send
```

or:

```
PUT /_synapse/admin/v1/server_announcements/{id}/send/{txnId}
```

Sends an event into the announcement room with configuration `id`, as the
management user. The event reaches every user that the room is delivered to.
No rate limit applies.

When using the `PUT` form, retransmissions with the same transaction ID will be
ignored in the same way as with `PUT
/_matrix/client/v3/rooms/{roomId}/send/{eventType}/{txnId}`. When Synapse
delegates authentication to the Matrix Authentication Service, the `PUT` form
needs an access token with a device.

The request body should look something like the following:

```json
{
    "content": {
        "msgtype": "m.text",
        "body": "Maintenance tonight at 22:00 UTC"
    }
}
```

You can optionally include the following additional parameter:

* `type`: the type of event. Defaults to `m.room.message`. Any non-state event
  type except `m.room.member` is accepted, so an announcement can be edited by
  sending an `m.room.message` with `m.new_content` and `m.relates_to`. To
  retract an announcement, send an `m.room.redaction` with the ID of the event
  to redact in `content`:

  ```json
  {
      "type": "m.room.redaction",
      "content": {"redacts": "$event_id"}
  }
  ```

State events cannot be sent: Synapse manages the room state from the
configuration, and a request with a `state_key` is rejected with
`M_INVALID_PARAM`.

Once the event has been sent, the API will return the following response:

```json
{
    "event_id": "<event_id>",
    "room_id": "<room_id>"
}
```

The API returns a 400 if no announcement rooms are configured, and a 404 if
`id` is not in the configuration.

Push notifications are not yet sent for announcements.
