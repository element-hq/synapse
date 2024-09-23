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

CREATE TABLE delayed_events_stream_pos (
    Lock CHAR(1) NOT NULL DEFAULT 'X' UNIQUE,  -- Makes sure this table only has one row.
    stream_id BIGINT NOT NULL,
    CHECK (Lock='X')
);

-- Start processing events from the point this migration was run, rather
-- than the beginning of time.
INSERT INTO delayed_events_stream_pos (
    stream_id
) SELECT COALESCE(MAX(stream_ordering), 0) from events;
