# Ratelimit callbacks

Ratelimit callbacks allow module developers to override ratelimit settings dynamically whilst
Synapse is running. Ratelimit callbacks can be registered using the module API's
`register_ratelimit_callbacks` method.

The available ratelimit callbacks are:

### `get_ratelimit_override_for_user`

_First introduced in Synapse v1.132.0_

```python
async def get_ratelimit_override_for_user(user: str, limiter_name: str) -> synapse.module_api.RatelimitOverride | None
```

**<span style="color:red">
Caution: This callback is currently experimental . The method signature or behaviour
may change without notice.
</span>**

Called when constructing a ratelimiter of a particular type for a user. The module can
return a `messages_per_second` and `burst_count` to be used, or `None` if
the default settings are adequate. The user is represented by their Matrix user ID
(e.g. `@alice:example.com`). The limiter name is usually taken from the `RatelimitSettings` key
value.

The limiters that are currently supported are:

- `rc_invites.per_room`
- `rc_invites.per_user`
- `rc_invites.per_issuer`

The `RatelimitOverride` return type has the following fields:

- `per_second: float`. The number of actions that can be performed in a second. `0.0` means that ratelimiting is disabled.
- `burst_count: int`. The number of actions that can be performed before being limited.

If multiple modules implement this callback, they will be considered in order. If a
callback returns `None`, Synapse falls through to the next one. The value of the first
callback that does not return `None` will be used. If this happens, Synapse will not call
any of the subsequent implementations of this callback. If no module returns a non-`None` value
then the default settings will be used.
