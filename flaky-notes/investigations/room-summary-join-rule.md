# `TestRoomSummaryAllowedRoomIDs/restricted_room_includes_allowed_room_ids`

Issue: https://github.com/element-hq/synapse/issues/19905 — #1 flaky Complement test
(87 failed jobs / 38 branches in a month, always `(workers, Postgres)`).

## Symptom

From `flaky-notes/data/joblogs/88350761590.log` (gotestfmt `❌` group):

```
room_summary_test.go:61: MatchResponse key 'join_rule' missing —
{"room_id":"!XZAB…:hs1","room_version":"8","num_joined_members":1,"world_readable":false,
 "guest_can_join":false,"allowed_room_ids":["!xwSpSUKpORBJMVCdbq:hs1"],"membership":"join"}
```

`allowed_room_ids` is present and correct; `join_rule` is absent entirely. The two
come from different sources, and only one of them is up to date.

## Root cause (confirmed)

`RoomSummaryHandler._build_room_entry` (`synapse/handlers/room_summary.py:772`) built the
entry from two sources:

- `join_rule` came from `stats.join_rules`
  (`synapse/handlers/room_summary.py:797` before the fix), where `stats` is
  `get_room_with_stats()` — a `LEFT JOIN room_stats_state` /
  `room_stats_current` query (`synapse/storage/databases/main/room.py:433-483`).
- `allowed_room_ids` came from **current state**: `get_current_state_ids()` filtered to
  `m.room.join_rules`, fed to `has_restricted_join_rules` /
  `get_rooms_that_allow_join` (`synapse/handlers/room_summary.py:810-829` before the fix;
  `synapse/handlers/event_auth.py:255-330`).

`room_stats_state` is written only by `StatsHandler`, which is wired up **only on the
process with `run_background_tasks`** and is driven off the current-state-delta stream via
replication callbacks (`synapse/handlers/stats.py:70-93`, `_unsafe_process` at
`synapse/handlers/stats.py:95`). Under the Complement worker deployment the room is created
on one worker, the `/room_summary` request is served by another, and the background worker
may not yet have processed the delta — so `room_stats_state` has no row (or a stale one) for
the room, `stats.join_rules` is `NULL`, and the `None`-filter at the end of
`_build_room_entry` drops the key. Current state, read live, is already correct, hence
`allowed_room_ids` being right while `join_rule` is missing. A monolith hits the same race
but the window is much smaller, which matches the failures being exclusive to `(workers, Postgres)`.

`num_joined_members: 1` in the failing response is not a counter-example: that column comes
from `room_stats_current`, which is updated in a different place from `room_stats_state`.

## Fix

`synapse/handlers/room_summary.py` — read `join_rule` from the join rules event in current
state, i.e. from the same `join_rules_state_ids` lookup that already backs
`allowed_room_ids`, and drop `stats.join_rules` from the entry. The two fields are now
sourced consistently and can no longer disagree.

No extra database round trip: the `get_current_state_ids` call was already there, and
`get_event` for the join rules event is cached, so the subsequent
`has_restricted_join_rules` / `get_rooms_that_allow_join` calls hit the event cache. This
matters because `_build_room_entry` is called once per room by the space-hierarchy endpoint.

Considered and rejected: the fallback proposed in the issue comments (only consult current
state when `stats.join_rules is None`). It leaves the stale-value case broken — a room whose
join rule has just *changed* has a non-NULL but wrong `room_stats_state` row — and keeps two
code paths for one field.

Tests (`tests/handlers/test_room_summary.py`, `RoomSummaryTestCase`):

- `test_join_rule_from_current_state` — clobbers `room_stats_state.join_rules` to `NULL`
  and then to a wrong value, asserts the summary still reports the live join rule.
- `test_join_rule_and_allowed_room_ids_are_consistent` — the Complement scenario
  (v8 restricted room allowing a space) with `room_stats_state.join_rules` nulled; asserts
  both `join_rule == restricted` and `allowed_room_ids == [space]`.

Both fail on unpatched code (`None != 'invite'`) and pass with the fix.

Changelog: `changelog.d/19905.bugfix`.

## Test results

Ran in the `syn-flaky-tests` workspace venv:

- `trial tests.handlers.test_room_summary` → 30 passed.
- `trial tests.handlers.test_room_summary tests.rest.client.test_rooms tests.handlers.test_stats tests.rest.admin.test_room`
  → 320 passed.
- `ruff format` / `ruff check` / `mypy` on both touched files → clean.

## Follow-up concerns

Everything else in the summary entry is still `room_stats_state`-derived and races the same
way (`synapse/handlers/room_summary.py:789-803`): `name`, `topic`, `canonical_alias`,
`avatar_url`, `room_type`, `encryption`, `world_readable`, `guest_can_join`. They were left
alone to keep this change surgical, but two are worth a look:

- `room_type` also decides whether `_summarize_local_room` recurses into children at all
  (`synapse/handlers/room_summary.py:519`) — a freshly created space can look like a plain
  room and return an empty hierarchy. There is already a cached
  `get_room_type()` with a create-event fallback (`synapse/storage/databases/main/state.py:388`,
  and `bulk_get_room_type` at `:325-386`) that would fix this cheaply.
- `world_readable` feeds `_is_remote_room_accessible`
  (`synapse/handlers/room_summary.py:750-755`) for federation responses, while the local
  accessibility check reads history visibility live — same inconsistency, opposite direction.

Other `room_stats_state` consumers with the same exposure: the public rooms directory and the
admin rooms API (`synapse/storage/databases/main/room.py:571, 722, 931, 947`). Staleness there
is far less user-visible. `get_room_type` / `get_room_encryption` already fall back to live
state, which is the precedent this fix follows.
