# Show reported rooms

This API returns information about reported rooms.

To use it, you will need to authenticate by providing an `access_token`
for a server admin: see [Admin API](../usage/administration/admin_api/).

The api is:
```
GET /_synapse/admin/v1/room_reports
```

It returns a JSON body like the following:

```json
{
    "room_reports": [
        {
            "id": 2,
            "received_ts": 1570897107409,
            "room_id": "!ERAgBpSOcCCuTJqQPk:matrix.org",
            "user_id": "@foo:matrix.org",
            "reason": "This room contains spam",
            "canonical_alias": "#alias1:matrix.org",
            "name": "Matrix chat",
            "topic": "Discussions about Matrix"
        },
        {
            "id": 3,
            "received_ts": 1598889612059,
            "room_id": "!eGvUQuTCkHGVwNMOjv:matrix.org",
            "user_id": "@bar:matrix.org",
            "reason": "Inappropriate content",
            "canonical_alias": null,
            "name": "Nefarious room",
            "topic": null
        }
    ],
    "next_batch": 2,
    "total": 4
}
```

Note: Reports for deleted or purged rooms are not returned. The endpoint returns reports in descending
chronological order.

To paginate, check for `next_batch` and if present, call the endpoint again with `from`
set to the value of `next_batch`. This will return a new page.

If the endpoint does not return a `next_batch` then there are no more reports to
paginate through.

**URL query parameters:**

* `limit`: positive integer - Optional. Used for pagination, denoting the maximum number
  of items to return in this call. Defaults to `100` if not provided.
* `from`: positive integer - Optional. Used for pagination, denoting the unix ms timestamp to return results
   from in descending order. Defaults to the current time. 
* `user_id`: string - Optional. Filter by the user ID of the reporter. This is the user who
  reported the room.
* `room_id`: string - Optional. Filter by room id.

**Response**

The following fields are returned in the JSON response body:

* `id`: integer - ID of room report.
* `received_ts`: integer - The timestamp (in milliseconds since the unix epoch) when this
  report was sent.
* `room_id`: string - The ID of the reported room.
* `user_id`: string - This is the user who reported the room.
* `reason`: string - Comment made by the `user_id` in this report indicating why the room
  was reported. May be blank or `null`.
* `canonical_alias`: string - The canonical alias of the room. `null` if the room does not
  have a canonical alias set.
* `name`: string - The name of the room.
* `topic`: string - The topic of the room. `null` if the room does not have a topic set.
* `next_batch`: integer - Indication for pagination. See above.
* `total`: integer - Total number of room reports related to the query
  (`user_id` and `room_id`).

# Show details of a specific room report

This API returns information about a specific room report.

The api is:
```
GET /_synapse/admin/v1/room_reports/<report_id>
```

It returns a JSON body like the following:

```json
{
    "id": 2,
    "received_ts": 1570897107409,
    "room_id": "!ERAgBpSOcCCuTJqQPk:matrix.org",
    "user_id": "@foo:matrix.org",
    "reason": "This room contains spam",
    "canonical_alias": "#alias1:matrix.org",
    "name": "Matrix HQ",
    "topic": "Discussions about Matrix"
}
```

**URL parameters:**

* `report_id`: string - The ID of the room report.

**Response**

The following fields are returned in the JSON response body:

* `id`: integer - ID of room report.
* `received_ts`: integer - The timestamp (in milliseconds since the unix epoch) when this
  report was sent.
* `room_id`: string - The ID of the reported room.
* `name`: string - The name of the room.
* `user_id`: string - This is the user who reported the room.
* `reason`: string - Comment made by the `user_id` in this report. May be blank.
* `canonical_alias`: string - The canonical alias of the room. `null` if the room does not
  have a canonical alias set.
* `topic`: string - The topic of the room. `null` if the room does not have a topic set.

# Delete a specific room report

This API deletes a specific room report. If the request is successful, the response body
will be an empty JSON object.

The api is:
```
DELETE /_synapse/admin/v1/room_reports/<report_id>
```

**URL parameters:**

* `report_id`: string - The ID of the room report to delete.