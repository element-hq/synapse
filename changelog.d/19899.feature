Implement MSC4497: allow filtering state events by type in the /state endpoint (experimental; requires `msc4497_state_event_type_filter`).
Requests to `/_matrix/client/v3/rooms/{roomId}/state` may include repeated `cc.koja.types` query parameters to limit returned state types.
