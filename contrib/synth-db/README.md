# synth-db

Profile a Synapse Postgres database into a small, anonymised dataset, and
rebuild a synthetic database from it. The synthetic database is meant for
exercising initial sync and sliding sync at realistic scale, so the profile
captures structure (who is in which rooms, how big rooms are, what state and
account data users carry) and event rates, not message history.

This crate is standalone: it does not depend on the `synapse` Rust crate and
is not a member of the `rust/` workspace. Build it from this directory:

```sh
cargo build --release
```

## Profiling

```sh
synth-db profile \
    --db postgres://synapse_ro:...@replica/synapse \
    --out ./profile-2026-10-14 \
    --salt-file ~/.synth-db-salt   # optional; see below
```

The tool only reads. Every session sets `default_transaction_read_only`, a
statement timeout (4 hours by default) and `work_mem` (512 MB). Point it at a
replica where you can: the rates query holds one index range scan open for
minutes on a busy server, and a replica with `hot_standby_feedback` will not
cancel it.

Expected cost on a ~300 GB database:

| section | what it reads | cost |
|---|---|---|
| `rooms` | `rooms` ⋈ `room_stats_*` ⋈ `sliding_sync_joined_rooms`, one indexed lookup per room for the create event | minutes |
| `users` | `users` plus one grouped pass over each per-user table (`devices`, `e2e_*`, `account_data`, `push_rules`, ...) | minutes |
| `memberships` | `local_current_membership` ⋈ `event_push_summary` ⋈ grouped `receipts_linearized`, `room_account_data`, `room_tags` | minutes |
| `room_detail` | two indexed queries on `current_state_events` per sampled room (`--sample-rooms`, default 2000) | seconds to minutes |
| `event_sizes` | `event_json TABLESAMPLE SYSTEM` sized to `--sample-events` (default 500k) rows, joined to `events` by primary key | minutes |
| `rates` | one range scan of `events` over the trailing `--window-days` (default 30) via the `events_ts` index | the long one; tens of minutes |

`events` and `event_json` are never scanned in full. Pass `--explain` to log
each section's query plan before it runs, and check that `rates` uses
`events_ts`.

Sections run concurrently (`--concurrency`, default 4 connections) and each
is written to its own `<section>.jsonl.zst`. With `--resume` and the same
`--salt-file`, a rerun skips finished sections.

### What leaves the database

The output is designed to be safe to copy out of the production environment.

| data | treatment |
|---|---|
| room, user, event and device IDs; server names | HMAC-SHA256 under a per-run random key, truncated to 64 bits. The key is never written unless you pass `--salt-file`, and then only to that file. |
| event, account-data and room types | spec (`m.*`) types verbatim; anything else replaced by `x.<hash>` |
| state keys | only member state keys are used, as user pseudonyms; other state keys are never read |
| names, topics, aliases, event content, filters, account data bodies, push rules | never read, except as `length(json)` for the event size histogram and a has-name / has-topic / has-avatar flag |
| membership, join rules, history visibility, encryption algorithm, room type, presence state | kept verbatim (low entropy, and sync behaviour depends on them) |
| timestamps | bucketed to the hour, relative to the profile time |
| stream orderings | kept as plain integers (they carry no content, and relative recency matters for sliding sync ordering) |

Rooms with no local member at all are dropped; they do not affect sync.

### Output format

```
meta.json               tool version, profile time, window, table size estimates, per-section row counts
rooms.jsonl.zst         one row per room: counts from room_stats, flags, last stream ordering
users.jsonl.zst         one row per local user: device/key/account-data/push-rule counts, presence
memberships.jsonl.zst   one row per (local user, room): membership, notification counts, receipt lag
room_detail.jsonl.zst   sampled rooms: non-member state by type, members by (server, membership)
event_sizes.jsonl.zst   log2 size histogram per (event type, is_state)
rates.jsonl.zst         per room: events per day over the window, by type bucket and sender locality, distinct senders
rates_global.jsonl.zst  server-wide events per hour, daily active users
```

Rows reference rooms and users by dense integer index (`idx` in `rooms` and
`users`, `r` / `u` elsewhere). `src/model.rs` is the authoritative description
of every field. `synth-db inspect <dir>` prints a summary.

## Generating

```sh
# 1. An empty database with the Synapse schema: point a homeserver at it
#    once, let the background updates finish (background_updates is empty),
#    stop it. Its server_name is what you pass below.
# 2.
synth-db generate \
    --profile ./profile-2026-10-14 \
    --db postgres://synapse:...@localhost/synapse_synth \
    --server-name synth.test
# 3. Start the homeserver again.
```

`--dry-run` prints the plan (rooms, users, events, `state_groups_state`
rows, highest stream ordering) and exits. `--scale N` generates N copies of
the profiled population; `--room-fraction` keeps a subset of rooms. Every
random choice derives from `--seed`, so the same inputs give the same
database.

What the synthetic server contains:

- One room per profiled room, at room version 11, with the recorded join
  rule, history visibility, encryption, name/topic/avatar/alias presence,
  non-member state (types from the sampled detail, filler for the rest),
  every local membership at its recorded stream position, and remote
  members drawn from per-server pools to match the recorded counts.
- A timeline of up to `--timeline-events` recent events per room, spaced
  by the room's recorded rate and mixed by type and sender locality as
  recorded. Threads, receipts, read markers, tags, room account data and
  notification counts follow the profile.
- State groups as Synapse would have made them: one delta per state event
  with a full snapshot every `--state-snapshot-every` (100) deltas. Large
  rooms produce many `state_groups_state` rows this way, as in production;
  the dry run reports the total.
- Events whose IDs are the correct reference hashes of their JSON, so
  Synapse accepts them. Signatures are placeholders.
- Every local user, with their devices, device/one-time/fallback/
  cross-signing keys, account data, push rules, filters, ignored users and
  presence. All users share the `--password`, and device `D` of user `N`
  already has the access token `synt_uN_dD`, so a load generator needs no
  login step.
- Derived tables filled directly rather than by background update:
  `room_stats_*`, `user_stats_current`, `sliding_sync_*`,
  `local_current_membership`, `user_directory`, `users_in_public_rooms`,
  `users_who_share_private_rooms`, `current_state_delta_stream`. Sequences
  and stream position markers are advanced past the generated data.

Stream orderings are the profile's own: a membership recorded at position
12345 lands at 12345, and other events are slotted in around them. That
keeps the relative recency of rooms, receipts and memberships exactly as
on the source server. Timestamps follow from the profile's event rate
(one stream unit = window / events in window).

Not generated: `pushers` (they would try to push), `user_directory_search`
(so directory search finds nothing), device list streams, remote device
caches, media, `event_search`. The generated history is shallow by design;
`/messages` reaches the room's creation within a few pages.

Generation runs three passes over the rooms (plan, then tables without a
foreign key onto `events`, then the rest) and replays the same seeded
decisions each time, so nothing is buffered. Rows go in through `COPY`,
one connection per table. The main cost is Postgres ingest.

## Testing

Unit tests need nothing:

```sh
cargo test
```

The end-to-end test runs `profile` against a real Synapse database and checks
that no raw room ID, user ID, event ID, device ID, alias, room name, topic or
server name appears anywhere in the output:

```sh
SYNTH_DB_TEST_URL=postgres://postgres:postgres@localhost/synapse cargo test
```

Any populated Synapse database will do. `fixture/populate.py` builds a
suitable one through the client API of a throwaway homeserver (users with
several devices and keys, a space, public rooms of varied size, encrypted
rooms, DMs, invites, leaves, a ban, threads, receipts, tags); it needs the
appservice in `fixture/appservice.yaml` registered so that messages can be
backdated across the rate window.

To check a generated database, start a homeserver on it and sync:

```sh
curl -H 'Authorization: Bearer synt_u0_d0' 'http://localhost:8008/_matrix/client/v3/sync?timeout=0'
```
