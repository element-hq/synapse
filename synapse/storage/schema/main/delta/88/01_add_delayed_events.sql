CREATE TABLE delayed_events (
    delay_id TEXT NOT NULL,
    user_localpart TEXT NOT NULL,
    device_id TEXT,
    delay BIGINT NOT NULL,
    send_ts BIGINT NOT NULL,
    room_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    state_key TEXT,
    origin_server_ts BIGINT,
    content bytea NOT NULL,
    is_processed BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (user_localpart, delay_id)
);

CREATE INDEX delayed_events_send_ts ON delayed_events (send_ts);
CREATE INDEX delayed_events_is_processed ON delayed_events (is_processed);
CREATE INDEX delayed_events_room_state_event_idx ON delayed_events (room_id, event_type, state_key) WHERE state_key IS NOT NULL;
