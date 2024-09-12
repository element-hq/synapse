# SCIM API

Synapse implement a basic subset of the SCIM 2.0 provisioning protocol as defined in [RFC7643](https://datatracker.ietf.org/doc/html/rfc7643) and [RFC7644](https://datatracker.ietf.org/doc/html/rfc7643).
This allows Identity Provider software to update user attributes in a standard and centralized way.

The SCIM endpoint is `/_matrix/client/unstable/coop.yaal/scim`.

## Installation

SCIM support for Synapse requires python 3.9+. The `matrix-synapse` package should be installed with the `scim` extra.

## Examples

### Create user

#### Request

```
POST /_matrix/client/unstable/coop.yaal/scim/Users
```

```json
{
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
    "userName": "bjensen",
    "externalId": "bjensen@test",
    "phoneNumbers": [{"value": "+1-12345678"}],
    "emails": [{"value": "bjensen@mydomain.tld"}],
    "photos": [
        {
            "type": "photo",
            "primary": true,
            "value": "https://mydomain.tld/photo.webp"
        }
    ],
    "displayName": "bjensen display name",
    "password": "correct horse battery staple"
}
```

#### Response

```json
{
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
    "meta": {
        "resourceType": "User",
        "created": "2024-07-22T16:59:16.326188+00:00",
        "lastModified": "2024-07-22T16:59:16.326188+00:00",
        "location": "https://synapse.example/_matrix/client/unstable/coop.yaal/scim/Users/@bjensen:test",
    },
    "id": "@bjensen:test",
    "externalId": "@bjensen:test",
    "phoneNumbers": [{"value": "+1-12345678"}],
    "userName": "bjensen",
    "emails": [{"value": "bjensen@mydomain.tld"}],
    "active": true,
    "photos": [
        {
            "type": "photo",
            "primary": true,
            "value": "https://mydomain.tld/photo.webp"
        }
    ],
    "displayName": "bjensen display name"
}
```

### Get user

#### Request

```
GET /_matrix/client/unstable/coop.yaal/scim/Users/@bjensen:test
```

#### Response

```json
{
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
    "meta": {
        "resourceType": "User",
        "created": "2024-07-22T16:59:16.326188+00:00",
        "lastModified": "2024-07-22T16:59:16.326188+00:00",
        "location": "https://synapse.example/_matrix/client/unstable/coop.yaal/scim/Users/@bjensen:test",
    },
    "id": "@bjensen:test",
    "externalId": "@bjensen:test",
    "phoneNumbers": [{"value": "+1-12345678"}],
    "userName": "bjensen",
    "emails": [{"value": "bjensen@mydomain.tld"}],
    "active": true,
    "photos": [
        {
            "type": "photo",
            "primary": true,
            "value": "https://mydomain.tld/photo.webp"
        }
    ],
    "displayName": "bjensen display name"
}
```

### Get users

#### Request

Note that requests can be paginated using the `startIndex` and the `count` query string parameters:

```
GET /_matrix/client/unstable/coop.yaal/scim/Users?startIndex=10&count=1
```

#### Response

```json
{
    "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
    "totalResults": 123,
    "Resources": [
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "meta": {
                "resourceType": "User",
                "created": "2024-07-22T16:59:16.326188+00:00",
                "lastModified": "2024-07-22T16:59:16.326188+00:00",
                "location": "https://synapse.example/_matrix/client/unstable/coop.yaal/scim/Users/@bjensen:test",
            },
            "id": "@bjensen:test",
            "externalId": "@bjensen:test",
            "phoneNumbers": [{"value": "+1-12345678"}],
            "userName": "bjensen",
            "emails": [{"value": "bjensen@mydomain.tld"}],
            "active": true,
            "photos": [
                {
                    "type": "photo",
                    "primary": true,
                    "value": "https://mydomain.tld/photo.webp"
                }
            ],
            "displayName": "bjensen display name"
        }
    ]
}
```

### Replace user

#### Request

```
PUT /_matrix/client/unstable/coop.yaal/scim/Users/@bjensen:test
```

```json
{
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
    "userName": "bjensen",
    "externalId": "bjensen@test",
    "phoneNumbers": [{"value": "+1-12345678"}],
    "emails": [{"value": "bjensen@mydomain.tld"}],
    "active": true,
    "photos": [
        {
            "type": "photo",
            "primary": true,
            "value": "https://mydomain.tld/photo.webp"
        }
    ],
    "displayName": "bjensen new display name",
    "password": "correct horse battery staple"
}
```

#### Response

```json
{
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
    "meta": {
        "resourceType": "User",
        "created": "2024-07-22T16:59:16.326188+00:00",
        "lastModified": "2024-07-22T17:34:12.834684+00:00",
        "location": "https://synapse.example/_matrix/client/unstable/coop.yaal/scim/Users/@bjensen:test",
    },
    "id": "@bjensen:test",
    "externalId": "@bjensen:test",
    "phoneNumbers": [{"value": "+1-12345678"}],
    "userName": "bjensen",
    "emails": [{"value": "bjensen@mydomain.tld"}],
    "active": true,
    "photos": [
        {
            "type": "photo",
            "primary": true,
            "value": "https://mydomain.tld/photo.webp"
        }
    ],
    "displayName": "bjensen new display name"
}
```

### Delete user

#### Request

```
DELETE /_matrix/client/unstable/coop.yaal/scim/Users/@bjensen:test
```

## Implementation details

### Models

The only SCIM resource type implemented is `User`, with the following attributes:
- `userName`
- `password`
- `emails`
- `phoneNumbers`
- `displayName`
- `photos` (as a MXC URI)
- `active`

The other SCIM User attributes will be ignored. Other resource types such as `Group` are not implemented.

### Endpoints

The implemented endpoints are:

- `/Users` (GET, POST)
- `/Users/<user_id>` (GET, PUT, DELETE)
- `/ServiceProviderConfig` (GET)
- `/Schemas` (GET)
- `/Schemas/<schema_id>` (GET)
- `/ResourceTypes` (GET)
- `/ResourceTypes/<resource_type_id>`

The following endpoints are not implemented:

- `/Users` (PATCH)
- [`/Me`](https://datatracker.ietf.org/doc/html/rfc7644#section-3.11) (GET, POST, PUT, PATCH, DELETE)
- `/Groups` (GET, POST, PUT, PATCH)
- [`/Bulk`](https://datatracker.ietf.org/doc/html/rfc7644#section-3.7) (POST)
- [`/.search`](https://datatracker.ietf.org/doc/html/rfc7644#section-3.4.3) (POST)

### Features

The following features are implemented:
- [pagination](https://datatracker.ietf.org/doc/html/rfc7644#section-3.4.2.4)

The following features are not implemented:
- [filtering](https://datatracker.ietf.org/doc/html/rfc7644#section-3.4.2.2)
- [sorting](https://datatracker.ietf.org/doc/html/rfc7644#section-3.4.2.3)
- [attributes selection](https://datatracker.ietf.org/doc/html/rfc7644#section-3.4.2.5)
- [ETags](https://datatracker.ietf.org/doc/html/rfc7644#section-3.14)