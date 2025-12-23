# Media repository callbacks

Media repository callbacks allow module developers to customise the behaviour of the
media repository on a per user basis. Media repository callbacks can be registered
using the module API's `register_media_repository_callbacks` method.

The available media repository callbacks are:

### `get_media_config_for_user`

_First introduced in Synapse v1.132.0_

```python
async def get_media_config_for_user(user_id: str) -> JsonDict | None
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

### `get_media_upload_limits_for_user`

_First introduced in Synapse v1.139.0_

```python
async def get_media_upload_limits_for_user(user_id: str, size: int) -> list[synapse.module_api.MediaUploadLimit] | None
```

**<span style="color:red">
Caution: This callback is currently experimental. The method signature or behaviour
may change without notice.
</span>**

Called when processing a request to store content in the media repository. This can be used to dynamically override
the [media upload limits configuration](../usage/configuration/config_documentation.html#media_upload_limits).

The arguments passed to this callback are:

* `user_id`: The Matrix user ID of the user (e.g. `@alice:example.com`) making the request.

If the callback returns a list then it will be used as the limits instead of those in the configuration (if any).

If an empty list is returned then no limits are applied (**warning:** users will be able
to upload as much data as they desire).

If multiple modules implement this callback, they will be considered in order. If a
callback returns `None`, Synapse falls through to the next one. The value of the first
callback that does not return `None` will be used. If this happens, Synapse will not call
any of the subsequent implementations of this callback.

If there are no registered modules, or if all modules return `None`, then
the default
[media upload limits configuration](../usage/configuration/config_documentation.html#media_upload_limits)
will be used.

### `on_media_upload_limit_exceeded`

_First introduced in Synapse v1.139.0_

```python
async def on_media_upload_limit_exceeded(user_id: str, limit: synapse.module_api.MediaUploadLimit, sent_bytes: int, attempted_bytes: int) -> None
```

**<span style="color:red">
Caution: This callback is currently experimental. The method signature or behaviour
may change without notice.
</span>**

Called when a user attempts to upload media that would exceed a
[configured media upload limit](../usage/configuration/config_documentation.html#media_upload_limits).

This callback will only be called on workers which handle
[POST /_matrix/media/v3/upload](https://spec.matrix.org/v1.15/client-server-api/#post_matrixmediav3upload)
requests.

This could be used to inform the user that they have reached a media upload limit through
some external method.

The arguments passed to this callback are:

* `user_id`: The Matrix user ID of the user (e.g. `@alice:example.com`) making the request.
* `limit`: The `synapse.module_api.MediaUploadLimit` representing the limit that was reached.
* `sent_bytes`: The number of bytes already sent during the period of the limit.
* `attempted_bytes`: The number of bytes that the user attempted to send.
