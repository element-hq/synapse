# Federation callbacks

Federation callbacks can be registered using the module API's `register_federation_callbacks` method.

## Callbacks

The available federation callbacks are:

### `on_event_delivered_over_federation`

_First introduced in Synapse v1.158.0_

```python
async def on_event_delivered_over_federation(
    event: FederationEventDeliveryEvent,
) -> None:
```

Called when an event has been delivered over federation.
See `FederationEventDeliveryEvent` for detailed information available on the event.

Note that depending on the specific method, delivery may not have
actually been acknowledged by the other homeserver.
See `FederatedEventDeliveryMethod` for details on which cases imply acknowledgment.

Modules should anticipate more methods being added to the `FederatedEventDeliveryMethod` enum
over time (it is non-exhaustive).

Only methods that deliver full, signed PDUs are included in this mechanism.
Some notable examples of excluded endpoints:
- `/send_knock` is excluded as it only returns unsigned 'stripped state'.
- `/timestamp_to_event` is excluded as it only returns event IDs, not events themselves.

If multiple modules implement this callback, Synapse runs them all in order.
Exceptions are logged and otherwise ignored.

Performance note:
- Registering this hook causes a performance (caching) optimisation on the
  Federation `/state` endpoint to be bypassed.
