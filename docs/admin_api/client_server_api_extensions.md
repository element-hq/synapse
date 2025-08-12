# Client-Server API Extensions

Server administrators can set special account data to change how the Client-Server API behaves for
their clients. Setting the account data, or having it already set, as a non-admin has no effect.

All configuration options can be set through the `io.element.synapse.admin_client_config` global 
account data on the admin's user account. 

Example:
```
PUT /_matrix/client/v3/user/{adminUserId}/account_data/io.element.synapse.admin_client_config
{
    "return_soft_failed_events": true
}
```

## See soft failed events

Learn more about soft failure from [the spec](https://spec.matrix.org/v1.14/server-server-api/#soft-failure).

To receive soft failed events in APIs like `/sync` and `/messages`, set `return_soft_failed_events`
to `true` in the admin client config. When `false`, the normal behaviour of these endpoints is to
exclude soft failed events.

Default: `false`
