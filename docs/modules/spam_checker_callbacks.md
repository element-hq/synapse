# Spam checker callbacks

Spam checker callbacks allow module developers to implement spam mitigation actions for
Synapse instances. Spam checker callbacks can be registered using the module API's
`register_spam_checker_callbacks` method.

## Callbacks

The available spam checker callbacks are:

### `check_event_for_spam`

_First introduced in Synapse v1.37.0_

_Changed in Synapse v1.60.0: `synapse.module_api.NOT_SPAM` and `synapse.module_api.errors.Codes` can be returned by this callback. Returning a boolean or a string is now deprecated._ 

```python
async def check_event_for_spam(event: "synapse.module_api.EventBase") -> Union["synapse.module_api.NOT_SPAM", "synapse.module_api.errors.Codes", str, bool]
```

Called when receiving an event from a client or via federation. The callback must return one of:
  - `synapse.module_api.NOT_SPAM`, to allow the operation. Other callbacks may still 
    decide to reject it.
  - `synapse.module_api.errors.Codes` to reject the operation with an error code. In case
    of doubt, `synapse.module_api.errors.Codes.FORBIDDEN` is a good error code.
  - (deprecated) a non-`Codes` `str` to reject the operation and specify an error message. Note that clients
    typically will not localize the error message to the user's preferred locale.
  - (deprecated) `False`, which is the same as returning `synapse.module_api.NOT_SPAM`.
  - (deprecated) `True`, which is the same as returning `synapse.module_api.errors.Codes.FORBIDDEN`.

If multiple modules implement this callback, they will be considered in order. If a
callback returns `synapse.module_api.NOT_SPAM`, Synapse falls through to the next one.
The value of the first callback that does not return `synapse.module_api.NOT_SPAM` will
be used. If this happens, Synapse will not call any of the subsequent implementations of
this callback.

### `user_may_join_room`

_First introduced in Synapse v1.37.0_

_Changed in Synapse v1.61.0: `synapse.module_api.NOT_SPAM` and `synapse.module_api.errors.Codes` can be returned by this callback. Returning a boolean is now deprecated._ 

```python
async def user_may_join_room(user: str, room: str, is_invited: bool) -> Union["synapse.module_api.NOT_SPAM", "synapse.module_api.errors.Codes", bool]
```

Called when a user is trying to join a room. The user is represented by their Matrix user ID (e.g.
`@alice:example.com`) and the room is represented by its Matrix ID (e.g.
`!room:example.com`). The module is also given a boolean to indicate whether the user
currently has a pending invite in the room.

This callback isn't called if the join is performed by a server administrator, or in the
context of a room creation.

The callback must return one of:
  - `synapse.module_api.NOT_SPAM`, to allow the operation. Other callbacks may still 
    decide to reject it.
  - `synapse.module_api.errors.Codes` to reject the operation with an error code. In case
    of doubt, `synapse.module_api.errors.Codes.FORBIDDEN` is a good error code.
  - (deprecated) `False`, which is the same as returning `synapse.module_api.NOT_SPAM`.
  - (deprecated) `True`, which is the same as returning `synapse.module_api.errors.Codes.FORBIDDEN`.

If multiple modules implement this callback, they will be considered in order. If a
callback returns `synapse.module_api.NOT_SPAM`, Synapse falls through to the next one.
The value of the first callback that does not return `synapse.module_api.NOT_SPAM` will
be used. If this happens, Synapse will not call any of the subsequent implementations of
this callback.

### `user_may_invite`

_First introduced in Synapse v1.37.0_

_Changed in Synapse v1.62.0: `synapse.module_api.NOT_SPAM` and `synapse.module_api.errors.Codes` can be returned by this callback. Returning a boolean is now deprecated._ 

```python
async def user_may_invite(inviter: str, invitee: str, room_id: str) -> Union["synapse.module_api.NOT_SPAM", "synapse.module_api.errors.Codes", bool]
```

Called when processing an invitation, both when one is created locally or when
receiving an invite over federation. Both inviter and invitee are represented by
their Matrix user ID (e.g. `@alice:example.com`).

Note that federated invites will call `federated_user_may_invite` before this callback.


The callback must return one of:
  - `synapse.module_api.NOT_SPAM`, to allow the operation. Other callbacks may still 
    decide to reject it.
  - `synapse.module_api.errors.Codes` to reject the operation with an error code. In case
    of doubt, `synapse.module_api.errors.Codes.FORBIDDEN` is a good error code.

  - (deprecated) `False`, which is the same as returning `synapse.module_api.NOT_SPAM`.
  - (deprecated) `True`, which is the same as returning `synapse.module_api.errors.Codes.FORBIDDEN`.

If multiple modules implement this callback, they will be considered in order. If a
callback returns `synapse.module_api.NOT_SPAM`, Synapse falls through to the next one.
The value of the first callback that does not return `synapse.module_api.NOT_SPAM` will
be used. If this happens, Synapse will not call any of the subsequent implementations of
this callback.


### `federated_user_may_invite`

_First introduced in Synapse v1.133.0_

```python
async def federated_user_may_invite(event: "synapse.events.EventBase") -> Union["synapse.module_api.NOT_SPAM", "synapse.module_api.errors.Codes", bool]
```

Called when processing an invitation received over federation. Unlike `user_may_invite`,
this callback receives the entire event, including any stripped state in the `unsigned`
section, not just the room and user IDs.

The callback must return one of:
  - `synapse.module_api.NOT_SPAM`, to allow the operation. Other callbacks may still 
    decide to reject it.
  - `synapse.module_api.errors.Codes` to reject the operation with an error code. In case
    of doubt, `synapse.module_api.errors.Codes.FORBIDDEN` is a good error code.

If multiple modules implement this callback, they will be considered in order. If a
callback returns `synapse.module_api.NOT_SPAM`, Synapse falls through to the next one.
The value of the first callback that does not return `synapse.module_api.NOT_SPAM` will
be used. If this happens, Synapse will not call any of the subsequent implementations of
this callback.

If all of the callbacks return `synapse.module_api.NOT_SPAM`, Synapse will also fall
through to the `user_may_invite` callback before approving the invite.


### `user_may_send_3pid_invite`

_First introduced in Synapse v1.45.0_

_Changed in Synapse v1.62.0: `synapse.module_api.NOT_SPAM` and `synapse.module_api.errors.Codes` can be returned by this callback. Returning a boolean is now deprecated._ 

```python
async def user_may_send_3pid_invite(
    inviter: str,
    medium: str,
    address: str,
    room_id: str,
) -> Union["synapse.module_api.NOT_SPAM", "synapse.module_api.errors.Codes", bool]
```

Called when processing an invitation using a third-party identifier (also called a 3PID,
e.g. an email address or a phone number). It is only called when a 3PID invite is created
locally - not when one is received in a room over federation. If the 3PID is already associated
with a Matrix ID, the spam check will go through the `user_may_invite` callback instead.

The inviter is represented by their Matrix user ID (e.g. `@alice:example.com`), and the
invitee is represented by its medium (e.g. "email") and its address
(e.g. `alice@example.com`). See [the Matrix specification](https://matrix.org/docs/spec/appendices#pid-types)
for more information regarding third-party identifiers.

For example, a call to this callback to send an invitation to the email address
`alice@example.com` would look like this:

```python
await user_may_send_3pid_invite(
    "@bob:example.com",  # The inviter's user ID
    "email",  # The medium of the 3PID to invite
    "alice@example.com",  # The address of the 3PID to invite
    "!some_room:example.com",  # The ID of the room to send the invite into
)
```

**Note**: If the third-party identifier is already associated with a matrix user ID,
[`user_may_invite`](#user_may_invite) will be used instead.

The callback must return one of:
  - `synapse.module_api.NOT_SPAM`, to allow the operation. Other callbacks may still 
    decide to reject it.
  - `synapse.module_api.errors.Codes` to reject the operation with an error code. In case
    of doubt, `synapse.module_api.errors.Codes.FORBIDDEN` is a good error code.

  - (deprecated) `False`, which is the same as returning `synapse.module_api.NOT_SPAM`.
  - (deprecated) `True`, which is the same as returning `synapse.module_api.errors.Codes.FORBIDDEN`.

If multiple modules implement this callback, they will be considered in order. If a
callback returns `synapse.module_api.NOT_SPAM`, Synapse falls through to the next one.
The value of the first callback that does not return `synapse.module_api.NOT_SPAM` will
be used. If this happens, Synapse will not call any of the subsequent implementations of
this callback.


### `user_may_create_room`

_First introduced in Synapse v1.37.0_

_Changed in Synapse v1.62.0: `synapse.module_api.NOT_SPAM` and `synapse.module_api.errors.Codes` can be returned by this callback. Returning a boolean is now deprecated._ 

_Changed in Synapse v1.132.0: Added the `room_config` argument. Callbacks that only expect a single `user_id` argument are still supported._

```python
async def user_may_create_room(user_id: str, room_config: synapse.module_api.JsonDict) -> Union["synapse.module_api.NOT_SPAM", "synapse.module_api.errors.Codes", bool]
```

Called when processing a room creation or room upgrade request.

The arguments passed to this callback are:

* `user_id`: The Matrix user ID of the user (e.g. `@alice:example.com`).
* `room_config`: The contents of the body of the [`/createRoom` request](https://spec.matrix.org/v1.15/client-server-api/#post_matrixclientv3createroom) as a dictionary.
  For a [room upgrade request](https://spec.matrix.org/v1.15/client-server-api/#post_matrixclientv3roomsroomidupgrade) it is a synthesised subset of what an equivalent
  `/createRoom` request would have looked like. Specifically, it contains the `creation_content` (linking to the previous room) and `initial_state` (containing a
  subset of the state of the previous room).

The callback must return one of:
  - `synapse.module_api.NOT_SPAM`, to allow the operation. Other callbacks may still 
    decide to reject it.
  - `synapse.module_api.errors.Codes` to reject the operation with an error code. In case
    of doubt, `synapse.module_api.errors.Codes.FORBIDDEN` is a good error code.

  - (deprecated) `False`, which is the same as returning `synapse.module_api.NOT_SPAM`.
  - (deprecated) `True`, which is the same as returning `synapse.module_api.errors.Codes.FORBIDDEN`.

If multiple modules implement this callback, they will be considered in order. If a
callback returns `synapse.module_api.NOT_SPAM`, Synapse falls through to the next one.
The value of the first callback that does not return `synapse.module_api.NOT_SPAM` will
be used. If this happens, Synapse will not call any of the subsequent implementations of
this callback.



### `user_may_create_room_alias`

_First introduced in Synapse v1.37.0_

_Changed in Synapse v1.62.0: `synapse.module_api.NOT_SPAM` and `synapse.module_api.errors.Codes` can be returned by this callback. Returning a boolean is now deprecated._ 

```python
async def user_may_create_room_alias(user_id: str, room_alias: "synapse.module_api.RoomAlias") -> Union["synapse.module_api.NOT_SPAM", "synapse.module_api.errors.Codes", bool]
```

Called when trying to associate an alias with an existing room.

The callback must return one of:
  - `synapse.module_api.NOT_SPAM`, to allow the operation. Other callbacks may still 
    decide to reject it.
  - `synapse.module_api.errors.Codes` to reject the operation with an error code. In case
    of doubt, `synapse.module_api.errors.Codes.FORBIDDEN` is a good error code.

  - (deprecated) `False`, which is the same as returning `synapse.module_api.NOT_SPAM`.
  - (deprecated) `True`, which is the same as returning `synapse.module_api.errors.Codes.FORBIDDEN`.

If multiple modules implement this callback, they will be considered in order. If a
callback returns `synapse.module_api.NOT_SPAM`, Synapse falls through to the next one.
The value of the first callback that does not return `synapse.module_api.NOT_SPAM` will
be used. If this happens, Synapse will not call any of the subsequent implementations of
this callback.



### `user_may_publish_room`

_First introduced in Synapse v1.37.0_

_Changed in Synapse v1.62.0: `synapse.module_api.NOT_SPAM` and `synapse.module_api.errors.Codes` can be returned by this callback. Returning a boolean is now deprecated._ 

```python
async def user_may_publish_room(user_id: str, room_id: str) -> Union["synapse.module_api.NOT_SPAM", "synapse.module_api.errors.Codes", bool]
```

Called when trying to publish a room to the homeserver's public rooms directory.

The callback must return one of:
  - `synapse.module_api.NOT_SPAM`, to allow the operation. Other callbacks may still 
    decide to reject it.
  - `synapse.module_api.errors.Codes` to reject the operation with an error code. In case
    of doubt, `synapse.module_api.errors.Codes.FORBIDDEN` is a good error code.

  - (deprecated) `False`, which is the same as returning `synapse.module_api.NOT_SPAM`.
  - (deprecated) `True`, which is the same as returning `synapse.module_api.errors.Codes.FORBIDDEN`.

If multiple modules implement this callback, they will be considered in order. If a
callback returns `synapse.module_api.NOT_SPAM`, Synapse falls through to the next one.
The value of the first callback that does not return `synapse.module_api.NOT_SPAM` will
be used. If this happens, Synapse will not call any of the subsequent implementations of
this callback.


### `user_may_send_state_event`

_First introduced in Synapse v1.132.0_

```python
async def user_may_send_state_event(user_id: str, room_id: str, event_type: str, state_key: str, content: JsonDict) -> Union["synapse.module_api.NOT_SPAM", "synapse.module_api.errors.Codes"]
```

**<span style="color:red">
Caution: This callback is currently experimental . The method signature or behaviour
may change without notice.
</span>**

Called when processing a request to [send state events](https://spec.matrix.org/latest/client-server-api/#put_matrixclientv3roomsroomidstateeventtypestatekey) to a room.

The arguments passed to this callback are:

* `user_id`: The Matrix user ID of the user (e.g. `@alice:example.com`) sending the state event.
* `room_id`: The ID of the room that the requested state event is being sent to.
* `event_type`: The requested type of event.
* `state_key`: The requested state key.
* `content`: The requested event contents.

The callback must return one of:
  - `synapse.module_api.NOT_SPAM`, to allow the operation. Other callbacks may still 
    decide to reject it.
  - `synapse.module_api.errors.Codes` to reject the operation with an error code. In case
    of doubt, `synapse.module_api.errors.Codes.FORBIDDEN` is a good error code.

If multiple modules implement this callback, they will be considered in order. If a
callback returns `synapse.module_api.NOT_SPAM`, Synapse falls through to the next one.
The value of the first callback that does not return `synapse.module_api.NOT_SPAM` will
be used. If this happens, Synapse will not call any of the subsequent implementations of
this callback.


### `check_username_for_spam`

_First introduced in Synapse v1.37.0_

```python
async def check_username_for_spam(user_profile: synapse.module_api.UserProfile, requester_id: str) -> bool
```

Called when computing search results in the user directory. The module must return a
`bool` indicating whether the given user should be excluded from user directory 
searches. Return `True` to indicate that the user is spammy and exclude them from 
search results; otherwise return `False`.

The profile is represented as a dictionary with the following keys:

* `user_id: str`. The Matrix ID for this user.
* `display_name: str | None`. The user's display name, or `None` if this user
  has not set a display name.
* `avatar_url: str | None`. The `mxc://` URL to the user's avatar, or `None`
  if this user has not set an avatar.

The module is given a copy of the original dictionary, so modifying it from within the
module cannot modify a user's profile when included in user directory search results.

The requester_id parameter is the ID of the user that called the user directory API.

If multiple modules implement this callback, they will be considered in order. If a
callback returns `False`, Synapse falls through to the next one. The value of the first
callback that does not return `False` will be used. If this happens, Synapse will not call
any of the subsequent implementations of this callback.

### `check_registration_for_spam`

_First introduced in Synapse v1.37.0_

```python
async def check_registration_for_spam(
    email_threepid: dict | None,
    username: str | None,
    request_info: Collection[Tuple[str, str]],
    auth_provider_id: str | None = None,
) -> "synapse.spam_checker_api.RegistrationBehaviour"
```

Called when registering a new user. The module must return a `RegistrationBehaviour`
indicating whether the registration can go through or must be denied, or whether the user
may be allowed to register but will be shadow banned.

The arguments passed to this callback are:

* `email_threepid`: The email address used for registering, if any.
* `username`: The username the user would like to register. Can be `None`, meaning that
  Synapse will generate one later.
* `request_info`: A collection of tuples, which first item is a user agent, and which
  second item is an IP address. These user agents and IP addresses are the ones that were
  used during the registration process.
* `auth_provider_id`: The identifier of the SSO authentication provider, if any.

If multiple modules implement this callback, they will be considered in order. If a
callback returns `RegistrationBehaviour.ALLOW`, Synapse falls through to the next one.
The value of the first callback that does not return `RegistrationBehaviour.ALLOW` will
be used. If this happens, Synapse will not call any of the subsequent implementations of
this callback.

### `check_media_file_for_spam`

_First introduced in Synapse v1.37.0_

_Changed in Synapse v1.62.0: `synapse.module_api.NOT_SPAM` and `synapse.module_api.errors.Codes` can be returned by this callback. Returning a boolean is now deprecated._ 

```python
async def check_media_file_for_spam(
    file_wrapper: "synapse.media.media_storage.ReadableFileWrapper",
    file_info: "synapse.media._base.FileInfo",
) -> Union["synapse.module_api.NOT_SPAM", "synapse.module_api.errors.Codes", bool]
```

Called when storing a local or remote file.

The callback must return one of:
  - `synapse.module_api.NOT_SPAM`, to allow the operation. Other callbacks may still 
    decide to reject it.
  - `synapse.module_api.errors.Codes` to reject the operation with an error code. In case
    of doubt, `synapse.module_api.errors.Codes.FORBIDDEN` is a good error code.

  - (deprecated) `False`, which is the same as returning `synapse.module_api.NOT_SPAM`.
  - (deprecated) `True`, which is the same as returning `synapse.module_api.errors.Codes.FORBIDDEN`.

If multiple modules implement this callback, they will be considered in order. If a
callback returns `synapse.module_api.NOT_SPAM`, Synapse falls through to the next one.
The value of the first callback that does not return `synapse.module_api.NOT_SPAM` will
be used. If this happens, Synapse will not call any of the subsequent implementations of
this callback.


### `should_drop_federated_event`

_First introduced in Synapse v1.60.0_

```python
async def should_drop_federated_event(event: "synapse.events.EventBase") -> bool
```

Called when checking whether a remote server can federate an event with us. **Returning
`True` from this function will silently drop a federated event and split-brain our view
of a room's DAG, and thus you shouldn't use this callback unless you know what you are
doing.**

If multiple modules implement this callback, they will be considered in order. If a
callback returns `False`, Synapse falls through to the next one. The value of the first
callback that does not return `False` will be used. If this happens, Synapse will not call
any of the subsequent implementations of this callback.

Note that this check is applied to federation invites as of Synapse v1.130.0.


### `check_login_for_spam`

_First introduced in Synapse v1.87.0_

```python
async def check_login_for_spam(
    user_id: str,
    device_id: str | None,
    initial_display_name: str | None,
    request_info: Collection[tuple[str | None, str]],
    auth_provider_id: str | None = None,
) -> Union["synapse.module_api.NOT_SPAM", "synapse.module_api.errors.Codes"]
```

Called when a user logs in.

The arguments passed to this callback are:

* `user_id`: The user ID the user is logging in with
* `device_id`: The device ID the user is re-logging into.
* `initial_display_name`: The device display name, if any.
* `request_info`: A collection of tuples, which first item is a user agent, and which
  second item is an IP address. These user agents and IP addresses are the ones that were
  used during the login process.
* `auth_provider_id`: The identifier of the SSO authentication provider, if any.

If multiple modules implement this callback, they will be considered in order. If a
callback returns `synapse.module_api.NOT_SPAM`, Synapse falls through to the next one.
The value of the first callback that does not return `synapse.module_api.NOT_SPAM` will
be used. If this happens, Synapse will not call any of the subsequent implementations of
this callback.

*Note:* This will not be called when a user registers.


## Example

The example below is a module that implements the spam checker callback
`check_event_for_spam` to deny any message sent by users whose Matrix user IDs are
mentioned in a configured list, and registers a web resource to the path
`/_synapse/client/list_spam_checker/is_evil` that returns a JSON object indicating
whether the provided user appears in that list.

```python
import json
from typing import Union

from twisted.web.resource import Resource
from twisted.web.server import Request

from synapse.module_api import ModuleApi


class IsUserEvilResource(Resource):
    def __init__(self, config):
        super(IsUserEvilResource, self).__init__()
        self.evil_users = config.get("evil_users") or []

    def render_GET(self, request: Request):
        user = request.args.get(b"user")[0].decode()
        request.setHeader(b"Content-Type", b"application/json")
        return json.dumps({"evil": user in self.evil_users}).encode()


class ListSpamChecker:
    def __init__(self, config: dict, api: ModuleApi):
        self.api = api
        self.evil_users = config.get("evil_users") or []

        self.api.register_spam_checker_callbacks(
            check_event_for_spam=self.check_event_for_spam,
        )

        self.api.register_web_resource(
            path="/_synapse/client/list_spam_checker/is_evil",
            resource=IsUserEvilResource(config),
        )

    async def check_event_for_spam(self, event: "synapse.events.EventBase") -> Literal["NOT_SPAM"] | Codes:
        if event.sender in self.evil_users:
          return Codes.FORBIDDEN
        else:
          return synapse.module_api.NOT_SPAM
```
