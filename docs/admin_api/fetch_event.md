# Fetch Event API

The fetch event API allows admins to fetch an event regardless of their membership in the room it
originated in.

To use it, you will need to authenticate by providing an `access_token`
for a server admin: see [Admin API](../usage/administration/admin_api/).

Request:
```http
GET /_synapse/admin/v1/fetch_event/<event_id>
```

The API returns a JSON body like the following:

Response:
```json
{
    "event": {
        "auth_events": [
            "$WhLChbYg6atHuFRP7cUd95naUtc8L0f7fqeizlsUVvc",
            "$9Wj8dt02lrNEWweeq-KjRABUYKba0K9DL2liRvsAdtQ",
            "$qJxBFxBt8_ODd9b3pgOL_jXP98S_igc1_kizuPSZFi4"
        ],
        "content": {
            "body": "Hey now",
            "msgtype": "m.text"
        },
        "depth": 6,
        "event_id": "$hJ_kcXbVMcI82JDrbqfUJIHu61tJD86uIFJ_8hNHi7s",
        "hashes": {
            "sha256": "LiNw8DtrRVf55EgAH8R42Wz7WCJUqGsPt2We6qZO5Rg"
        },
        "origin_server_ts": 799,
        "prev_events": [
            "$cnSUrNMnC3Ywh9_W7EquFxYQjC_sT3BAAVzcUVxZq1g"
        ],
        "room_id": "!aIhKToCqgPTBloWMpf:test",
        "sender": "@user:test",
        "signatures": {
            "test": {
                "ed25519:a_lPym": "7mqSDwK1k7rnw34Dd8Fahu0rhPW7jPmcWPRtRDoEN9Yuv+BCM2+Rfdpv2MjxNKy3AYDEBwUwYEuaKMBaEMiKAQ"
            }
        },
        "type": "m.room.message",
        "unsigned": {
            "age_ts": 799
        }
    }
}
```


