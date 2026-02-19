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

**Note**: If the policy server flagged the event as spam and that caused soft failure, that will be indicated
in the event's `unsigned` content like so:

```json
{
  "type": "m.room.message",
  "other": "event_fields_go_here",
  "unsigned": {
    "io.element.synapse.soft_failed": true,
    "io.element.synapse.policy_server_spammy": true
  }
}
```

Default: `false`

## See events marked spammy by policy servers

Learn more about policy servers from [MSC4284](https://github.com/matrix-org/matrix-spec-proposals/pull/4284).

Similar to `return_soft_failed_events`, clients logged in with admin accounts can see events which were
flagged by the policy server as spammy (and thus soft failed) by setting `return_policy_server_spammy_events`
to `true`.

`return_policy_server_spammy_events` may be `true` while `return_soft_failed_events` is `false` to only see
policy server-flagged events. When `return_soft_failed_events` is `true` however, `return_policy_server_spammy_events`
is always `true`.

Events which were flagged by the policy will be flagged as `io.element.synapse.policy_server_spammy` in the
event's `unsigned` content, like so:

```json
{
  "type": "m.room.message",
  "other": "event_fields_go_here",
  "unsigned": {
    "io.element.synapse.soft_failed": true,
    "io.element.synapse.policy_server_spammy": true
  }
}
```

Default: `true` if `return_soft_failed_events` is `true`, otherwise `false`
