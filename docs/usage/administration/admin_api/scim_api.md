# SCIM API

Synapse implements a basic subset of the SCIM 2.0 provisioning protocol as defined in [RFC7643](https://datatracker.ietf.org/doc/html/rfc7643) and [RFC7644](https://datatracker.ietf.org/doc/html/rfc7643).
This allows Identity Provider software to update user attributes in a standard and centralized way.

The SCIM endpoint is `/_synapse/admin/scim/v2`.

<div class="warning">

The synapse SCIM API is an experimental feature, and it is disabled by default.
It might be removed someday in favor of an implementation in the [Matrix Authentication Service](https://github.com/element-hq/matrix-authentication-service).

</div>

## Installation

SCIM support for Synapse requires python 3.9+. The `matrix-synapse` package should be installed with the `scim` extra. e.g. with `pip install matrix-synapse[scim]`. For compatibility reasons, the SCIM support cannot be included in the `all` extra, so you need to explicitly use the `scim` extra to enable the API.

Then it must be explicitly enabled by configuration:

```yaml
experimental_features:
    scim:
      enabled: true
      idp_id: <my-provider>
```

## Examples

This sections presents examples of SCIM requests and responses that are supported by the synapse implementation.
Tools like [scim2-cli](https://scim2-cli.readthedocs.io) can be used to manually build payloads and send requests to the SCIM endpoint.

### Create user

#### Request

```
POST /_synapse/admin/scim/v2/Users
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
        "location": "https://synapse.example/_synapse/admin/scim/v2/Users/@bjensen:test",
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
GET /_synapse/admin/scim/v2/Users/@bjensen:test
```

#### Response

```json
{
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
    "meta": {
        "resourceType": "User",
        "created": "2024-07-22T16:59:16.326188+00:00",
        "lastModified": "2024-07-22T16:59:16.326188+00:00",
        "location": "https://synapse.example/_synapse/admin/scim/v2/Users/@bjensen:test",
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
GET /_synapse/admin/scim/v2/Users?startIndex=10&count=1
```

<div class="warning">

For performances reason, the page count will be maxed to 1000.

</div>

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
                "location": "https://synapse.example/_synapse/admin/scim/v2/Users/@bjensen:test",
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
PUT /_synapse/admin/scim/v2/Users/@bjensen:test
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
        "location": "https://synapse.example/_synapse/admin/scim/v2/Users/@bjensen:test",
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

User deletion requests [deactivate](../../../admin_api/user_admin_api.md) users, with the `erase` option.

#### Request

```
DELETE /_synapse/admin/scim/v2/Users/@bjensen:test
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
