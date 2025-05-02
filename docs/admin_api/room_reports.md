# Show reported rooms

This API returns information about reported rooms.

To use it, you will need to authenticate by providing an `access_token`
for a server admin: see [Admin API](../usage/administration/admin_api/).

The api is:
```
GET /_synapse/admin/v1/room_reports?from=0&limit=10
```

It returns a JSON body like the following:

```json
{
    "room_reports": [
        {
            "id": 2,
            "reason": "foo",
            "received_ts": 1570897107409,
            "canonical_alias": "#alias1:matrix.org",
            "room_id": "!ERAgBpSOcCCuTJqQPk:matrix.org",
            "name": "Matrix HQ",
            "user_id": "@foo:matrix.org"
        },
        {
            "id": 3,
            "reason": "bar",
            "received_ts": 1598889612059,
            "canonical_alias": "#alias2:matrix.org",
            "room_id": "!eGvUQuTCkHGVwNMOjv:matrix.org",
            "name": "Your room name here",
            "user_id": "@bar:matrix.org"
        }
    ],
    "next_token": 2,
    "total": 4
}
```

To paginate, check for `next_token` and if present, call the endpoint again with `from`
set to the value of `next_token` and the same `limit`. This will return a new page.

If the endpoint does not return a `next_token` then there are no more reports to
paginate through.

**Query parameters:**

* `limit`: integer - Is optional but is used for pagination, denoting the maximum number
  of items to return in this call. Defaults to `100`.
* `from`: integer - Is optional but used for pagination, denoting the offset in the
  returned results. This should be treated as an opaque value and not explicitly set to
  anything other than the return value of `next_token` from a previous call. Defaults to `0`.
* `dir`: string - Direction of event report order. Whether to fetch the most recent
  first (`b`) or the oldest first (`f`). Defaults to `b`.
* `user_id`: optional string - Filter by the user ID of the reporter. This is the user who reported the event
   and wrote the reason.
* `room_id`: optional string - Filter by (reported) room id.

**Response**

The following fields are returned in the JSON response body:

* `id`: integer - ID of room report.
* `received_ts`: integer - The timestamp (in milliseconds since the unix epoch) when this
  report was sent.
* `room_id`: string - The ID of the room being reported.
* `name`: string - The name of the room.
* `user_id`: string - This is the user who reported the room and wrote the reason.
* `reason`: string - Comment made by the `user_id` in this report. May be blank or `null`.
* `canonical_alias`: string - The canonical alias of the room. `null` if the room does not
  have a canonical alias set.
* `next_token`: integer - Indication for pagination. See above.
* `total`: integer - Total number of room reports related to the query
  (`user_id` and `room_id`).
