# Media repository callbacks

Media repository callbacks allow module developers to customise the behaviour of the
media repository on a per user basis. Media repository callbacks can be registered
using the module API's `register_media_repository_callbacks` method.

The available media repository callbacks are:

### `get_media_config_for_user`

_First introduced in Synapse v1.X.X_

```python
async def get_media_config_for_user(user: str) -> Optional[JsonDict]
```

Called when processing a request from a client for the configuration of the content
repository. The module can return a JSON dictionary that should be returned for the use
or `None` if the module is happy for the default dictionary to be used. The user is
represented by their Matrix user ID (e.g. `@alice:example.com`).

If multiple modules implement this callback, they will be considered in order. If a
callback returns `None`, Synapse falls through to the next one. The value of the first
callback that does not return `None` will be used. If this happens, Synapse will not call
any of the subsequent implementations of this callback.

If no module returns a non-`None` value then the default configuration will be returned.

### `is_user_allowed_to_upload_media_of_size`

_First introduced in Synapse v1.X.X_

```python
async def is_user_allowed_to_upload_media_of_size(user: str, size: int) -> bool
```

Called before media is accepted for upload from a user, in case the module needs to
enforce a different limit for the particular user. The user is represented by their Matrix
user ID. The size is in bytes.

If the module returns `False`, the current request will be denied with the error code
`M_TOO_LARGE` and the HTTP status code 413.

If multiple modules implement this callback, they will be considered in order. If a callback
returns `True`, Synapse falls through to the next one. The value of the first callback that
returns `False` will be used. If this happens, Synapse will not call any of the subsequent
implementations of this callback.
