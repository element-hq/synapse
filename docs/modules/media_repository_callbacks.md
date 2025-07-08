# Media repository callbacks

Media repository callbacks allow module developers to customise the behaviour of the
media repository on a per user basis. Media repository callbacks can be registered
using the module API's `register_media_repository_callbacks` method.

The available media repository callbacks are:

### `get_media_config_for_user`

_First introduced in Synapse v1.132.0_

```python
async def get_media_config_for_user(user_id: str) -> Optional[JsonDict]
```

**<span style="color:red">
Caution: This callback is currently experimental . The method signature or behaviour
may change without notice.
</span>**

Called when processing a request from a client for the
[media config endpoint](https://spec.matrix.org/latest/client-server-api/#get_matrixclientv1mediaconfig).

The arguments passed to this callback are:

* `user_id`: The Matrix user ID of the user (e.g. `@alice:example.com`) making the request.

If the callback returns a dictionary then it will be used as the body of the response to the
client.

If multiple modules implement this callback, they will be considered in order. If a
callback returns `None`, Synapse falls through to the next one. The value of the first
callback that does not return `None` will be used. If this happens, Synapse will not call
any of the subsequent implementations of this callback.

If no module returns a non-`None` value then the default media config will be returned.

### `is_user_allowed_to_upload_media_of_size`

_First introduced in Synapse v1.132.0_

```python
async def is_user_allowed_to_upload_media_of_size(user_id: str, size: int) -> bool
```

**<span style="color:red">
Caution: This callback is currently experimental . The method signature or behaviour
may change without notice.
</span>**

Called before media is accepted for upload from a user, in case the module needs to
enforce a different limit for the particular user.

The arguments passed to this callback are:

* `user_id`: The Matrix user ID of the user (e.g. `@alice:example.com`) making the request.
* `size`: The size in bytes of media that is being requested to upload.

If the module returns `False`, the current request will be denied with the error code
`M_TOO_LARGE` and the HTTP status code 413.

If multiple modules implement this callback, they will be considered in order. If a callback
returns `True`, Synapse falls through to the next one. The value of the first callback that
returns `False` will be used. If this happens, Synapse will not call any of the subsequent
implementations of this callback.
