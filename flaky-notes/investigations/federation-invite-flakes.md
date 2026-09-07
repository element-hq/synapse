# `TestFederationRoomsInvite` flakes — investigation

**Verdict: five real Synapse bugs, four of them previously unreported. Not
Complement timing.** A Complement-side wait/retry would mask a user-visible `403`
on a legitimate `/leave`, a `500` on `/sync`, a rescinded invite that is
*permanently* invisible to the client, and a leave event that is silently never
federated. Do not do it.

Biggest single finding: **`@cachedList` swallows exceptions from its inner
function and silently returns only the already-cached subset**
(`synapse/util/caches/descriptors.py:433-434`) — repo-wide, not specific to this
test. It is what turns a would-be 500 into the mode-A 403.

Scope: 18 failed jobs / 7 branches / 11 days (2026-07-20 → 2026-08-13), from
`data/summary.json`. **18/18 in `(workers, Postgres)`**; zero in either monolith
arrangement. Tracking issue:
[#19858](https://github.com/element-hq/synapse/issues/19858).

Sources: `data/joblogs/<job_id>.log`; Complement `tests/federation_rooms_invite_test.go`
(CI checkout is offset **−2 lines** vs. the current `matrix-org/complement` HEAD:
log `…_test.go:168` == local line 170); Synapse `develop` @ ~v1.158–v1.159.

---

## 1. Summary

Every one of these subtests exercises **out-of-band membership events**
(`outlier=True`, `out_of_band_membership=True`) — invites delivered over
`PUT /_matrix/federation/v2/invite`, and leaves generated or received out of
band — often combined with **faster joins** (partial state), always on
**workers**. Five independent defects fall out of that combination:

| # | Failure mode | Jobs | Subtests | Status |
|---|---|---|---|---|
| **A** | `403 M_FORBIDDEN "No create event in auth events"` from `POST /rooms/{id}/leave` | 4 | `Remote…already participating` | **New bug — file it.** Non-idempotent `PartialStateConflictError` retry |
| **B** | `500` from `GET /sync` — `Exception: Missing state for event that is not user's own membership` | 4 | `…several_times` ×2, `Non-invitee…` ×1, `Inviter…rescind` ×1 | Diagnosed = [#19858](https://github.com/element-hq/synapse/issues/19858); **fix open, unmerged**: [PR #19960](https://github.com/element-hq/synapse/pull/19960) |
| **C** | `rooms.leave.<room>` never appears in `/sync` — *permanently* | 5 | `Inviter…rescind` | **New bug — file it.** Initial-sync path drops out-of-band leaves |
| **D** | bob2's leave is **never federated to hs1 at all** | 1 | `Remote…already participating` | **New bug — file it.** Federation sender computes destinations from partial state just after the flag was cleared |
| **E** | a room bob is *joined* to is absent from all his syncs | 1 | `Non-invitee…` | Unresolved; needs a repro / extra logging |
| — | log truncated, no assertion captured | 3 | — | 92712976094, 92999582900, 92999804556 |

Five distinct defects, four of them new. **A** and **D** are opened by the
faster-join **un-partial-stating** window; **B** and **C** by **outlier /
out-of-band membership** handling. Every one is user-visible and permanent, not a
delay: a `403` on a legal `/leave`, a `500` on `/sync`, a rescinded invite that
never disappears from the client, and a leave that never reaches the other
server.

Mode A additionally exposes a **repo-wide correctness bug**: `@cachedList`
swallows exceptions raised by its inner function and returns only the
already-cached subset (`synapse/util/caches/descriptors.py:433-434`). See §2.

**Cross-contamination matters.** All 8 subtests use `t.Parallel()` and share the
*same four users* (`federation_rooms_invite_test.go:28-31`). `bob`'s `/sync` on
hs2 covers every room from every subtest, so one poisoned room makes whichever
subtest is currently polling `/sync` fail. That is why mode B shows up under
`…several_times` and `Non-invitee…` even though those scenarios never make hs2
join the room — the *broken* room comes from a sibling subtest. Attributing
modes to subtests is only meaningful for mode A and D, which fail on the HTTP
request itself.

---

## 2. Mode A — `403 "No create event in auth events"` on `/leave`

Jobs: `91784332435`, `92330586006`, `89316428974`, `94417426041`. All
`Remote_invited_user_can_reject_invite_when_homeserver_is_already_participating_in_the_room`.

```
client.go:260: CSAPI.Must: POST .../rooms/%21eAVgpXQhjFDGYzddsG:hs1/leave
  returned non-2xx code: 403 Forbidden
  body: {"errcode":"M_FORBIDDEN","error":"No create event in auth events"}
```

### Scenario (test lines 132-154)

1. alice (hs1) creates a private room.
2. alice invites bob (hs2); bob **joins** → hs2 is now participating, via a
   **faster join** (`send_join?omit_members=true`) → room is *partial state* on hs2.
3. alice invites bob2 (hs2). hs2 receives that invite **twice**: once via
   `PUT /_matrix/federation/v2/invite/...` (stored as an **outlier /
   out-of-band membership**, on `federation_reader1`) and once as a normal PDU in
   a `/send` transaction (on `federation_inbound1`), which de-outliers it.
4. bob2 `POST /rooms/{id}/leave` → hits `event_creator1` → 403.

### Confirmed signature — present in **all four** jobs

The 403 is always immediately preceded, in the same client request, by a
**`PartialStateConflictError` (409)** from the event persister, i.e. the local
leave was built while the room was still partial-state but the room got
un-partial-stated before the persist landed. Job `94417426041`, room
`!eAVgpXQhjFDGYzddsG:hs1`, hs2:

```
10:39:04,153 master           | remote_join: @user-60-bob:hs2 into room: !eAVgpXQ…   (faster join)
10:39:04,744 master           | sync_partial_state_room-6 - Syncing state for room !eAVgpXQ… via hs1
10:39:05,060 federation_reader1| 200 PUT /_matrix/federation/v2/invite/…/$EEgtFf…    (bob2 invite → OUTLIER)
10:39:05,208 federation_inbound1| handling received PDU …<Event $EEgtFf…, m.room.member, @user-61-bob2:hs2, invite>
10:39:05,254 master           | sync_partial_state_room-6 - Updating current state for !eAVgpXQ…
10:39:05,271 master           | sync_partial_state_room-6 - Clearing partial-state flag for !eAVgpXQ…
10:39:05,279 master           | sync_partial_state_room-6 - State resync complete for !eAVgpXQ…
10:39:05,629 event_persister1 | send_events: Got batch of 1 events to persist to rooms {'!eAVgpXQ…'}
10:39:05,647 event_persister1 | Cannot persist events ['$tFnhjuRj…'] in rooms ['!eAVgpXQ…']: room has been un-partial stated
10:39:05,653 event_creator1   | POST-158 Received response to …/replication/send_events/CQqaDDoBSg: 409
10:39:05,669 event_creator1   | POST-158 Denying new event <Event $v3qJTH8x…, m.room.member,
                                 state_key=@user-61-bob2:hs2, membership=leave> because 403: No create event in auth events
10:39:05,671 event_creator1   | POST-158 SynapseError: 403 - No create event in auth events
```

Note the **two different event IDs**: `$tFnhjuRj…` (attempt 1, built correctly,
killed by the 409) and `$v3qJTH8x…` (attempt 2, built *wrong*). The other three
jobs are byte-for-byte the same shape:

| job | 409 on `send_events` | then 403 deny | partial-state clear just before |
|---|---|---|---|
| 94417426041 | `…/send_events/CQqaDDoBSg: 409` @10:39:05,653 | @10:39:05,669 | @10:39:05,271 |
| 91784332435 | `…/send_events/aXLjxjYCJQ: 409` @19:07:31,118 | @19:07:31,145 | @19:07:31,068 |
| 92330586006 | `…/send_events/tuwDoYsbMz: 409` @14:20:03,560 | @14:20:03,586 | @14:20:03,508 |
| 89316428974 | `…/send_events/cqbcpyMYNE: 409` @20:08:52,586 | @20:08:52,601 | @20:08:52,534 |

### Code path

- `synapse/rest/client/room.py` → `RoomMemberHandler.update_membership` →
  `update_membership_locked` (`synapse/handlers/room_member.py:940+`).
- `synapse/handlers/room_member.py:975` — `latest_event_ids = await self.store.get_prev_events_for_room(room_id)`
  (forward extremities; **this list object is then reused for every retry**).
- `:977-984` — `partial_state_before_join = compute_state_after_events(..., await_full_state=False)`,
  `is_host_in_room = await self._is_host_in_room(...)`. Here `is_host_in_room == True`
  (bob is joined), so we do **not** take the `remote_reject_invite` out-of-band
  path at `:1167`; we fall through to the local path.
- `synapse/handlers/room_member.py:1219` — `_local_membership_update(..., prev_event_ids=latest_event_ids, ...)`.
- `synapse/handlers/room_member.py:474-535` — the retry loop:
  ```python
  max_retries = 5
  for i in range(max_retries):
      try:
          event, unpersisted_context = await self.event_creation_handler.create_event(
              ..., prev_event_ids=prev_event_ids, ...)      # <- SAME list each iteration
          context = await unpersisted_context.persist(event)
          result_event = await ...handle_new_client_event(...)
          break
      except PartialStateConflictError as e:
          # "context needs to be recomputed, so let's do so"
          if i == max_retries - 1:
              raise e
  ```
  Nothing between iterations is recomputed except by re-calling `create_event`.
- `synapse/handlers/message.py:1247+` `create_new_client_event` /
  `_create_new_client_event` → `synapse/events/builder.py:130` `EventBuilder.build`.
- `synapse/events/builder.py:197-212` — with `auth_event_ids is None` (the normal
  case), auth events are derived from
  `compute_state_after_events(room_id, prev_event_ids, state_filter=auth_types_for_event(...), await_full_state=False)`
  then `compute_auth_events`.
- `synapse/events/builder.py:230-252` — the out-of-band-membership fix-up added in
  [#18075](https://github.com/element-hq/synapse/pull/18075) ("Fix join being denied
  after being invited over federation"), whose docstring describes *exactly* this
  scenario:
  ```python
  if self.type == EventTypes.Member and self.is_mine_id(self.state_key):
      _membership, member_event_id = await self._store.get_local_current_membership_for_user_in_room(...)
      if member_event_id is not None and member_event_id not in auth_event_ids:
          auth_event_ids.append(member_event_id)
          prev_event_ids.append(member_event_id)   # <-- MUTATES THE CALLER'S LIST
  ```
- The event is then rejected at `synapse/handlers/message.py:1624-1628`
  (`check_auth_rules_from_context` → `AuthError` → `logger.warning("Denying new event …")`),
  raised from `synapse/event_auth.py:341-343`:
  ```python
  creation_event = auth_dict.get((EventTypes.Create, ""), None)
  if not creation_event:
      raise AuthError(403, "No create event in auth events")
  ```

### Root cause — three bugs stacked, **reproduced locally**

The room is v11 (`default_room_version = "11"`, `synapse/config/server.py:179`;
room IDs still carry `:hs1`, so neither MSC4291 nor MSC4242 state-DAGs apply).
The retried event ends up with `auth_events == [<bob2's out-of-band invite>]` and
nothing else — that single entry passes rules 2.1–2.3 of
`check_state_independent_auth_rules` (a member event for the event's own
`state_key` *is* an expected auth type) and dies at rule 2.4. So
`compute_auth_events` was handed an **empty state map**.

**1. `EventBuilder.build` mutates its caller's `prev_event_ids` list**
(`synapse/events/builder.py:252`). Object identity, with no copy anywhere along
the way: `room_member.py:975` (`get_prev_events_for_room`) → `:1226`
`_local_membership_update(prev_event_ids=…)` → `:495` inside the retry loop →
`message.py:752` → `:1415` → `builder.build(prev_event_ids=<same list>)`. So
attempt 1 appends bob2's out-of-band invite outlier to the list that attempt 2
then uses as its `prev_events`.

**2. `@cachedList` silently swallows exceptions from its inner DB function and
returns partial results.** This is the load-bearing one, and it is a *general*
Synapse correctness bug, not specific to this code path.
`synapse/util/caches/descriptors.py:433-444`:

```python
def errback_all(f: Failure) -> None:
    cache_entry.error_bulk(cache, missing, f)      # returns None
...
missing_d = defer.maybeDeferred(preserve_fn(self.orig), **args_to_call
            ).addCallbacks(complete_all, errback_all)
...
d = defer.gatherResults(cached_defers, consumeErrors=True).addCallbacks(
        lambda _: results, unwrapFirstError)       # :448-449
```

A Twisted errback that returns a non-`Failure` **consumes** the failure, so
`missing_d` resolves *successfully*, `gatherResults` succeeds, and `lambda _:
results` hands back `results` — which contains **only the keys that were already
in the cache**. Every key that went to the DB in that batch is silently dropped.

Consequently `_get_state_group_for_events`' documented guard
(`synapse/storage/databases/main/state.py:630-632`,
`raise RuntimeError("No state group for unknown or outlier event %s")`) — and
the matching contract on `resolve_state_groups_for_events`
(`synapse/state/__init__.py:502-504`, "Raises RuntimeError if we don't have a
state group for one or more of the events") — **never fires for the caller and
never appears in the logs.** That is why we see a `403` and not a `500`, and why
no `RuntimeError` is anywhere in any of the four job logs.

Verified live against this checkout:
`get_state_group_for_events([extremity, oob_invite_outlier])` returns
`{extremity: 2}` when the extremity is cached, and **`{}`** when it is not.

**3. Un-partial-stating cold-starts the cache between the two attempts.**
`_update_state_for_partial_state_event_txn` invalidates
`_get_state_group_for_event` **per event id** (writer side
`synapse/storage/databases/main/state.py:717-721`; worker side, over the
`un_partial_stated_event_stream` replication stream, `state.py:114`) — including
the forward extremity `X`. So on attempt 2 *both* `X` and the invite outlier are
cache misses, the single inner DB call raises, and (bug 2) the caller gets `{}`.

### The chain

```
attempt 1: prev=[X]                → state at X ok → auth=[create,PL,JR] (+invite appended)
           builder.py:252          → prev list MUTATED to [X, invite_outlier]
           persist                 → 409 PartialStateConflictError (room un-partial-stated)
   (meanwhile) un-partial-stating invalidates _get_state_group_for_event for X
attempt 2: prev=[X, invite_outlier]  (bug 1)
           _get_state_group_for_events → RuntimeError (outlier) → SWALLOWED (bug 2) → {}
           state/__init__.py:531-532  → len(state_group_ids_set)==0 → _StateCacheEntry(state={})
           compute_auth_events({})    → []
           builder.py:242-249         → auth_events = [invite_outlier]
           event_auth.py:341-343      → 403 "No create event in auth events"
```

Reproduced by driving
`tests/federation/test_federation_out_of_band_membership.py` through the retry
with the cache invalidated in between → the same
`403 M_FORBIDDEN "No create event in auth events"`.

Ruled out along the way: a cached `None` state group (would raise
`Exception("One of state, state_group or prev_group must be not None")` at
`state/__init__.py:106-107` → a 500, not a 403); empty `prev_event_ids` (blocked
by the assert at `message.py:1389`, and no `AssertionError` in the logs); a
deleted state group (`state/bg_updates.py:121` does seed `{}` silently, but
state-group deletion has a 10-minute delay — implausible in CI).

Confidence: **high**. The trigger is confirmed in 4/4 jobs; the mechanism is
reproduced and each step is verifiable in the source.

### Why workers-only

`PartialStateConflictError` is surfaced as a **409 over the
`/_synapse/replication/send_events` HTTP replication endpoint**
(`event_creator1` ← `event_persister1`). In a monolith the persist happens
in-process and the same race window (client request on one worker, faster-join
resync on `master`, PDU handling on `federation_inbound1`, persist on
`event_persister1`) does not open in the same way.

---

## 3. Mode B — `500` on `/sync`, "Missing state for event that is not user's own membership"

Jobs: `88362171804`, `89288509852` (`…several_times`), `88609731713`
(`Non-invitee…`), `89326353716` (`Inviter…rescind`).

```
federation_rooms_invite_test.go:60: CSAPI.Must: GET .../_matrix/client/v3/sync?timeout=1000
  returned non-2xx code: 500 Internal Server Error - {"errcode":"M_UNKNOWN","error":"Internal server error"}
```

This is exactly [#19858](https://github.com/element-hq/synapse/issues/19858), and
it is **already diagnosed** (erikjohnston's comment on the issue) and **already
has an open PR**: [#19960](https://github.com/element-hq/synapse/pull/19960)
("Don't 500 /sync when an always-included event has no state", by @ara4n, opened
2026-07-14, still unmerged as of 2026-08-13). That is why it keeps recurring.

Mechanism, for the record:

- `synapse/handlers/sync.py:770-792` — `_load_filtered_recents` passes
  `always_include_ids=current_state_ids`, the subset of timeline **state** events
  that are in `current_state_events`.
- `synapse/visibility.py:422` — `_check_client_allowed_to_see_event` returns the
  event early for `always_include_ids`, **before** the outlier guard at `:424-436`.
- `synapse/visibility.py:771` — `_get_effective_room_visibility_from_state`
  deliberately excludes outliers from the state fetch, so `state_after_event is None`.
- `synapse/visibility.py:221-228` — for an event that is neither the syncing
  user's own membership nor has state, the "unreachable" branch fires.

The reachable path is the **outlier → non-outlier transition** of an out-of-band
invite that also arrives as a normal PDU. The window is tight and consistent —
in all four jobs the 500 lands **20–45 ms after** the persister logs
`_update_outliers_txn: Updating state for ex-outlier event`, always on
`synchrotron1`, always `SyncRestServlet`:

| job | event | `De-outliering` | `_update_outliers_txn` | 500 | Δ |
|---|---|---|---|---|---|
| 88362171804 | `$JliWB8Dwt…` | 13:32:00,550 | 13:32:01,167 | 13:32:01,190 | 23 ms |
| 88609731713 | `$eY3Yc…` | 10:54:36,126 | 10:54:36,670 | 10:54:36,692 | 22 ms |
| 89288509852 | `$7Kk3W…` | 18:10:45,482 | 18:10:45,856 | 18:10:45,893 | 37 ms |
| 89326353716 | `$MMyRc…` | 20:51:51,456 | 20:51:51,928 | 20:51:51,971 | 43 ms |

In that window the synchrotron sees the event as `outlier=False` (so it lands in
`always_include_ids` via `current_state_events`) but has no state group for it
yet, so `state_after_event is None` and the "unreachable" branch fires. Sample
(88362171804):

```
federation_reader1  | 13:32:00,426 PUT /_matrix/federation/v2/invite/%21mxdWOuHNwMsxJPFtMP%3Ahs1/%24JliWB8Dwt…   (→ outlier)
federation_inbound1 | 13:32:00,546 handling received PDU in room !mxdWOuHNwMsxJPFtMP:hs1: $JliWB8Dwt…            (same event via /send)
federation_inbound1 | 13:32:00,550 - federation_event.py:227 - De-outliering event $JliWB8Dwt…
event_persister2    | 13:32:01,167 - events.py:2661 - _update_outliers_txn: Updating state for ex-outlier event $JliWB8Dwt…
synchrotron1        | 13:32:01,190 - ERROR - GET-212 - Failed handle request via 'SyncRestServlet'
                       … sync.py:2796 → sync.py:854 → visibility.py:242 → visibility.py:228
                       Exception: Missing state for event that is not user's own membership
```

Note: `PartialStateConflictError`/`un-partial stated` 409s *do* appear in all
four logs, but minutes earlier and in unrelated rooms — **not** the trigger here.
Mode B is a de-outliering race, not an un-partial-stating race.

Note also the `not user's own membership` condition: the event blowing up bob's
sync is *bob2*'s membership — i.e. it comes from a sibling parallel subtest's room.

Real-world impact is confirmed, not just CI: MadLittleMods linked two production
Sentry issues on #19858.

---

## 4. Mode C — `rooms.leave.<room>` never appears in `/sync`

Jobs: `89021934386`, `89028977155`, `88694821018`, `93832057394`, `91196710939` —
all `Inviter_user_can_rescind_invite_over_federation`. (`88608082529` looks
superficially similar but is a different bug: see mode D, §5.)

```
federation_rooms_invite_test.go:168: @user-60-bob:hs2 MustSyncUntil: timed out after 5.03s. Seen 12 /sync responses.
  Response #N: syncMembershipIn(@user-60-bob:hs2, !kWqGmEGYsNyDsVODqX:hs1, leave):
    Key rooms.leave.!kWqGmEGYsNyDsVODqX:hs1.state.events does not exist
  & Key rooms.leave.!kWqGmEGYsNyDsVODqX:hs1.timeline.events does not exist
```

The wording matters: **"does not exist"** (as opposed to "did not pass while
iterating over 0 elements", which is what `88608082529` reports) means the
**entire `rooms.leave.<roomID>` entry is absent** from the sync response, not
merely empty — see `complement/client/sync.go:317-400` (`syncMembershipIn` →
`checkArrayElements`). That distinction is what separates mode C from mode D.

### Scenario

`Inviter user can rescind invite over federation` (test lines 156-171): alice
invites bob, then alice **kicks** bob while hs2 is *not* in the room. hs2 only
ever holds an out-of-band invite for bob.

- Sender side, hs1: `synapse/federation/sender/__init__.py:672-694` adds bob's
  server to the destinations only because bob's **invite is in the kick's
  `auth_events`**.
- Receiver side, hs2: `synapse/handlers/federation_event.py:258-296` — the
  "leave event rescinding an invite" path, added in
  [#18823](https://github.com/element-hq/synapse/pull/18823) (Sep 2025, alongside
  the Complement test in complement#797). It persists the rescission as
  `outlier = True; out_of_band_membership = True`.
### Root cause: the **incremental** sync path special-cases out-of-band leaves; the **initial** sync path does not

For a room where hs2 holds *only* out-of-band membership outliers (hs2 never
joined), there is nothing to build an archived room entry out of:

- the timeline is empty — every timeline/pagination query filters outliers
  (`synapse/storage/databases/main/stream.py:2352`, `WHERE event.outlier = FALSE`);
- the state is empty — `StateStorageController.get_state_ids_at`
  (`synapse/storage/controllers/state.py:443-471`) calls
  `get_last_event_id_in_room_before_stream_ordering`, whose SQL also filters
  outliers (`synapse/storage/databases/main/stream.py:1644`, `AND NOT outlier`);
  it finds nothing, logs `Failed to find any events in room …` (`:467`) and
  returns `{}`;
- an `ArchivedSyncResult` that is empty on all three of timeline/state/account_data
  is **falsy** (`sync.py:200-204`) and is therefore dropped at `sync.py:3353`
  (`if archived_room_sync or always_include:`; `always_include` is only the
  request-level `?full_state=true`, `sync.py:2695`).

**The incremental path knows this and works around it.** `_get_rooms_changed`,
`synapse/handlers/sync.py:2907-2924`:

```python
if leave_event.internal_metadata.is_out_of_band_membership():
    batch_events: list[EventBase] | None = [leave_event]   # inject by hand
else:
    batch_events = None
room_entries.append(RoomSyncResultBuilder(
    ..., rtype="archived", events=batch_events,
    out_of_band=leave_event.internal_metadata.is_out_of_band_membership()))
```

**The initial-sync path does not.** `_get_all_rooms` ("Like `_get_rooms_changed`,
but assumes the `since_token` is `None`", `sync.py:2994`) builds the archived
entry at `sync.py:3053-3066` with a flat `events=None` and no `out_of_band`:

```python
elif event.membership in (Membership.LEAVE, Membership.BAN):
    ...
    room_entries.append(RoomSyncResultBuilder(
        room_id=event.room_id, rtype="archived",
        events=None,                # <-- no out-of-band injection
        newly_joined=False, full_state=True,
        since_token=since_token, upto_token=leave_token, end_token=leave_token))
        #                        ^ out_of_band defaults to False
```

So an initial sync of such a room produces empty timeline + empty state → the
room is dropped from `rooms.leave` entirely.

And the incremental path can never rescue it afterwards, because of
`sync.py:2886-2894`:

```python
# If the leave event happened before the since token then we bail.
if since_token and not leave_position.persisted_after(since_token.room_key):
    continue
```

### The race

- **Normally**: bob's `MustSyncUntil` issues its initial sync *before* hs1's
  rescission PDU reaches hs2. Response #1 has no leave; response #2+ are
  incremental with `since` < leave, so `_get_rooms_changed` injects the
  out-of-band leave and the test passes.
- **On failure**: the PDU beats the client. hs1's `POST /kick` takes ~1s to
  return (event creation + replication + persist), while the federation
  transaction to hs2 goes out and is persisted *before* that response lands —
  so bob's first sync is already past the leave. The initial sync silently drops
  the room, every subsequent incremental sync `continue`s past it, and **bob can
  never learn the invite was rescinded**. Not a delay: a permanent loss. That is
  exactly why the test burns all 12 sync responses instead of eventually passing.

Workers make the losing ordering much more likely: on hs1 the client request
fans out over replication (`event_creator1` → `event_persister` → notifier →
`federation_sender`) so the HTTP response is slow relative to federation
delivery, whereas a monolith answers the client almost immediately.

### Evidence (job `93832057394`, room `!GZZGeetfWsIzZXqMwv:hs1`, hs2)

```
15:46:06,821 federation_reader1| 200 PUT /_matrix/federation/v2/invite/…/$wfuvdAGdB83FX…   (bob invite → OUTLIER)
15:46:06,941 synchrotron1      | GET-194 User membership change between getting rooms and current token:
                                  @user-60-bob:hs2 invite !GZZGeetfWsIzZXqMwv:hs1        (sync.py:2037)
15:46:09,170 federation_inbound1| handling received PDU …<$vjutvgUEBXcnee5rNUDSvM5tNfhrLHKGlTjroRU4-tk, m.room.member,…>
15:46:09,185 event_persister2  | fed_send_events: Got batch of 1 events to persist to room !GZZGeetfWsIzZXqMwv:hs1
15:46:09,361 synchrotron1      | GET-198 Failed to find any events in room !GZZGeetfWsIzZXqMwv:hs1
                                  at RoomStreamToken(stream: 65, …)                       (storage/controllers/state.py:467)
… the same line for GET-199, 204, 207, 208, 214, 219, 222 through 15:46:12,936 …
```

Timing check: hs2 handled and persisted the rescission at **09,170 / 09,185**,
while hs1's `POST /kick` only returned at **09,262** (`event_creator1 POST-288
… 1.057sec … 200 "POST /_matrix/client/v3/rooms/%21GZZGeetfWsIzZXqMwv:hs1/kick"`).
Complement only starts `bob.MustSyncUntil` after that response, so bob's *first*
(initial) sync is already past the leave — the losing ordering. The recurring
`Failed to find any events in room … at RoomStreamToken(stream: 65)` on
`synchrotron1` (GET-198, 199, 204, 207, 208, 214, 219, 222) is the empty-state
computation for the archived entry that then gets dropped.

The same shape holds in the other four jobs, with the leave persisted 170–390 ms
*before* bob's first `since`-less sync:

| job | room | leave persisted | stream | first sync `Failed to find any events` |
|---|---|---|---|---|
| 89021934386 | `!kWqGmEGYsNyDsVODqX:hs1` | 19:16:05,748 | 62 | 19:16:05,915 (`GET-195`) |
| 89028977155 | `!AgRPPpIqFzMwiLEbgd:hs1` | 19:46:50,300 | 58 | 19:46:50,572 (`GET-189`) |
| 88694821018 | `!ddjUNuBtdNCKCRWJmE:hs1` | 16:40:21,811 | 66 | 16:40:22,199 (`GET-198`) |
| 93832057394 | `!GZZGeetfWsIzZXqMwv:hs1` | 15:46:09,185 | 65 | 15:46:09,361 (`GET-198`) |

The initial sync's `next_batch` (`s62` / `s58` / `s66` / `s65`) is already **at**
the leave's stream position, which is precisely what condemns every later
incremental sync at `sync.py:2886-2894`.

**Partial state is not involved in mode C**: none of these room IDs sees a
`make_join` / `send_join` / `sync_partial_state_room` — hs2 only ever holds the
out-of-band invite and the out-of-band rescission.

Confidence: **high** (5/5 jobs, unambiguous code asymmetry between
`_get_rooms_changed` and `_get_all_rooms`, timing evidence matches in all five).

---

## 5. Mode D — bob2's leave is never federated to hs1

Job: `88608082529`,
`Remote_invited_user_can_reject_invite_when_homeserver_is_already_participating_in_the_room`,
at `federation_rooms_invite_test.go:151` (local :153) — this is *alice* timing
out, not bob2:

```
@user-58-alice:hs1 MustSyncUntil: timed out after 5.127375176s. Seen 6 /sync responses.
  syncMembershipIn(@user-61-bob2:hs2, !HzHwbungyAzZavNEsS:hs1, leave):
  check function did not pass while iterating over 0 elements: []
  & … over 9 elements: [ … ]
```

Alice is joined, so the room *is* present in her sync (9 state events) — bob2's
leave simply never arrived at hs1. And indeed hs2 **never sent it**. hs2, room
`!HzHwbungyAzZavNEsS:hs1`:

```
10:46:58,277 federation_reader1 | PUT /_matrix/federation/v2/send_join/…?omit_members=true      (faster join)
10:46:58,462 master             | federation.py:1944 sync_partial_state_room-6 - Syncing state for room !HzHwbung…
10:46:58,718 event_persister2   | send_events - Got batch of 1 events …  ← bob2's leave $1tw75CJ…
10:46:58,764 event_creator1     | 200 "POST /_matrix/client/v3/rooms/%21HzHwbungyAzZavNEsS:hs1/leave"
10:46:58,857 master             | federation_event.py:665 - Updating state for $1tw75CJvmjw8DEZ5hwB87D-A8dCS5aG74v7ttGv6WII
10:46:58,945 master             | federation.py:1966 - Clearing partial-state flag for !HzHwbungyAzZavNEsS:hs1
10:46:58,954 master             | federation.py:1970 - State resync complete for !HzHwbungyAzZavNEsS:hs1
10:46:59,017 federation_sender1 | sender/__init__.py:650 - Unexpectedly did not have cached prev group for $1tw75CJ…
```

After that line **no transaction is ever sent to hs1** (last TX at 10:46:58,496;
`federation_sender1` then goes idle until SIGTERM at 10:47:08), and hs1's log
contains no PDU for `$1tw75CJ…` at all.

### Root cause

`FederationSender._process_event_queue_loop` destination lookup,
`synapse/federation/sender/__init__.py:613-664`, tries in order:

1. `get_partial_state_servers_at_join(room_id)` — now `None`: the row was deleted
   72 ms earlier by `Clearing partial-state flag`;
2. the `event_to_prev_state_group` external cache — miss, hence the logged
   `Unexpectedly did not have cached prev group for …` (`:650`);
3. `get_hosts_in_room_at_events(room_id, event.prev_event_ids())`
   (`synapse/state/__init__.py:243-256`) — resolves the state *at the leave's
   prev events*, i.e. the `omit_members=true` partial state, which does not
   contain `@user-58-alice:hs1`.

Destinations therefore collapse to hs2 itself and the leave is dropped on the
floor. Silently: no error, no retry, no queue entry.

This is arguably the most serious of the five — a membership event that is
accepted from the client with `200 OK` and then never federated, with no
subsequent repair path. The window is small (72 ms here) but the consequence is
permanent divergence between the two servers.

Confidence: **high** — the `Unexpectedly did not have cached prev group` log line
plus the absence of any outbound transaction is conclusive.

---

## 6. Mode E — a joined room is absent from every one of bob's syncs

Job: `88371206565`, `Non-invitee_user_cannot_rescind_invite_over_federation`,
`federation_rooms_invite_test.go:215` (local :217):

```
:207: SendEventSynced waiting for event ID $vTYNLlLvk4HAIB0vQw1T88CLkpkXaojkEYfkzwORaO4
:215: @user-60-bob:hs2 MustSyncUntil: timed out after 5.685336589s. Seen 10 /sync responses.
  Response #1..#10: SyncTimelineHas(!mXrMdNKvBdqnYYiDME:hs1):
    Key rooms.join.!mXrMdNKvBdqnYYiDME:hs1.timeline.events does not exist
```

Note this failure has nothing to do with the rescission the subtest is named
after — it is the follow-up "can bob still see messages in room1" step.

hs2 faster-joined room1 and finished the resync 2.4 s before the message, then
received and persisted the message normally (not an outlier, not soft-failed):

```
14:08:59,945 federation_reader1 | PUT /_matrix/federation/v2/send_join/%21mXrMdNKvBdqnYYiDME%3Ahs1/…?omit_members=true
14:09:00,995 master             | Clearing partial-state flag for !mXrMdNKvBdqnYYiDME:hs1
14:09:01,018 master             | State resync complete
14:09:03,430 federation_inbound1| handling received PDU …: $vTYNLlLvk4… (m.room.message)
14:09:03,916 event_persister2   | POST-176 - Got batch of 1 events to persist …          → events stream 72
```

Matching the 10 failing responses to the synchrotron access log by duration gives
bob's token chain `s56 → s70 → s72 → s74 → s76 → s77 → s78 → s79`; the
`since=s70` request (`GET-203`, 14:09:04,296→04,636) spans `(70, 72]` and so
covered stream 72, yet returned neither the message nor the room. **The room is
absent from all 10 responses** even though bob joined it 3 s earlier.

Leading hypothesis (inference, not proven by a log line): room1 was missing from
`sync_result_builder.joined_room_ids` on `synchrotron1`. That alone explains a
*persistent* absence, because `_get_rooms_changed` takes the shortcut
`if not non_joins: continue` (`synapse/handlers/sync.py:2821`) for a room whose
only membership change is a join, so the room falls through both the
membership-change path and the `for room_id in joined_room_ids` path. The
stale-membership workaround at `sync.py:2037` did fire once for this room
(`14:09:00,465 GET-190 - User membership change between getting rooms and current
token: @user-60-bob:hs2 join !mXrMdNKvBdqnYYiDME:hs1`) but only covers changes
inside the current token window, so it stops helping once the join is in the past.

Confidence: **low-medium**. Needs a repro or a debug log of
`sync_result_builder.joined_room_ids`.

---

## 7. Recommended fixes

Ordered by value.

1. **Fix `@cachedList` swallowing exceptions** —
   `synapse/util/caches/descriptors.py:433-434`. `errback_all` must re-raise (or
   return) the `Failure` so it propagates through `gatherResults` /
   `unwrapFirstError` instead of being converted into a silent partial result.
   This is the load-bearing bug behind mode A, but it is **not** specific to it:
   *every* `@cachedList` in Synapse whose inner function can raise currently
   degrades to "return only what was already cached". Grep for `@cachedList`
   before/after — this changes error behaviour repo-wide, so it wants its own PR
   and careful review, but the current behaviour turns documented `RuntimeError`
   contracts (e.g. `synapse/storage/databases/main/state.py:630-632`,
   `synapse/state/__init__.py:502-504`) into silent data corruption.

2. **Stop `EventBuilder.build` mutating its caller's lists** —
   `synapse/events/builder.py:249-252` should append to a local copy
   (`prev_event_ids = list(prev_event_ids)`), and likewise for a caller-supplied
   `auth_event_ids`. This is the safer *local* fix for mode A and is worth doing
   even after (1): callers legitimately reuse `prev_event_ids` (the
   `PartialStateConflictError` retry at `synapse/handlers/room_member.py:474-535`,
   and `synapse/handlers/message.py:1370-1372` which can pass
   `get_prev_events_for_room`'s result straight through).

3. **Make the `PartialStateConflictError` retry actually recompute** —
   `synapse/handlers/room_member.py:474-535`. The handler's own comment says
   "context needs to be recomputed", but `latest_event_ids`, `is_host_in_room`
   and `partial_state_before_join` are all computed once at `:975-984`, *outside*
   the loop, even though the whole premise of the retry is that the room's
   partial-state status changed. The same pattern at
   `synapse/handlers/message.py:1236-1240` should be audited.
   Defensive extra: `build()` should refuse to emit a non-create event whose
   computed `auth_event_ids` lacks `(m.room.create, "")` and raise something
   retryable, rather than handing the user a 403. Cf. the sibling sanity assert
   at `synapse/handlers/message.py:1384-1390`, added for exactly this
   "fails with a somewhat confusing 'No create event in auth events'" reason.
   Note [#19045](https://github.com/element-hq/synapse/issues/19045): real users
   stuck unable to leave rooms with this exact error, and unable to purge them
   either. Same error string, possibly a different root cause — worth checking
   whether this fixes it.

4. **Merge [PR #19960](https://github.com/element-hq/synapse/pull/19960)** —
   kills mode B outright (4 of the 15 classified jobs), already reviewed, open since
   2026-07-14. Cheapest win available; fixes nothing else.

5. **Mode C — teach the initial-sync path about out-of-band leaves.** Small and
   self-contained: `_get_all_rooms` (`synapse/handlers/sync.py:3053-3066`) must do
   what `_get_rooms_changed` (`:2907-2924`) already does — fetch the leave event
   and, if `internal_metadata.is_out_of_band_membership()`, pass
   `events=[leave_event]` and `out_of_band=True` into the `RoomSyncResultBuilder`.
   `_get_all_rooms` iterates `RoomsForUser` rows from
   `get_rooms_for_local_user_where_membership_is` (`sync.py:3009`) and already
   does `await self.store.get_event(event.event_id)` in the `INVITE`/`KNOCK`
   branches (`:3043`, `:3046`) — the `LEAVE`/`BAN` branch just doesn't.
   More robust variant: for archived rooms with empty timeline *and* empty state,
   fall back to the membership event from `local_current_membership`, which *is*
   written for these events
   (`synapse/storage/databases/main/events.py:3245-3275`). Check whether sliding
   sync shares the bug — it has its own `sliding_sync_membership_snapshots` table
   (same function) and may not.

6. **Mode D — don't compute federation destinations from partial state that has
   just been superseded.** `synapse/federation/sender/__init__.py:613-664`: when
   `get_partial_state_servers_at_join` returns `None` *and* the prev-state-group
   cache misses, the fallback to `get_hosts_in_room_at_events(prev_event_ids)`
   silently returns the pre-resync host set. For an event created while the room
   was partial-state but sent after un-partial-stating, that host set is wrong and
   the event is dropped with no error. Options: use the room's *current* hosts
   when the room is no longer partial-stated; or treat the
   `Unexpectedly did not have cached prev group` case (`:650`) as a hard error /
   retry rather than a log line.

7. **Mode E — instrument before fixing.** Add a debug log of
   `sync_result_builder.joined_room_ids` (or of the
   `if not non_joins: continue` shortcut at `synapse/handlers/sync.py:2821`) and
   re-run; there is not enough in the current logs to be sure.

8. **Regression tests.** `tests/federation/test_federation_out_of_band_membership.py`
   already has the scaffolding used to reproduce mode A. Natural additions:
   mode A → assert the retried leave still carries `m.room.create` in
   `auth_events`; `@cachedList` → assert an inner exception propagates rather
   than yielding partial results (`tests/util/caches/test_descriptors.py`);
   mode C → a sync test asserting an out-of-band leave is visible on an
   *initial* sync.

9. **Do NOT** add a retry/wait to `federation_rooms_invite_test.go`. Every mode
   here is a hard, permanent, user-visible failure — a 403 on a legal `/leave`, a
   500 on `/sync`, a rescinded invite that never disappears, a leave that never
   crosses the federation. Nothing here is "arrives late": the `/sync` loops
   already poll for 5 s across 6–12 responses and would have caught any mere
   delay.

## 8. Confidence summary

| Claim | Confidence |
|---|---|
| All 18 failures are workers-only and centre on out-of-band membership events, faster joins, or both | **High** |
| Mode A is triggered by the `PartialStateConflictError` retry | **High** — 4/4 jobs, identical 409-then-403 signature |
| Mode A mechanism = `builder.py:252` list mutation + `@cachedList` swallowing the outlier `RuntimeError` + un-partial-state cache invalidation | **High** — reproduced locally; `get_state_group_for_events([extremity, outlier])` returns `{}` when the extremity is cold |
| `@cachedList` (`descriptors.py:433-434`) returns partial results instead of raising, repo-wide | **High** — verified by reading; `errback_all` returns `None`, consuming the `Failure` |
| Mode B == #19858; trigger is the de-outliering window (20–45 ms after `_update_outliers_txn`), **not** un-partial-stating | **High** — 4/4 jobs, consistent timing |
| Mode B is fixed by unmerged #19960 | **High** |
| Mode C = `_get_all_rooms` lacks the out-of-band-leave injection `_get_rooms_changed` has, so an initial sync landing after the leave drops the room *forever* | **High** — 5/5 jobs, leave persisted 170–390 ms before the first sync, `next_batch` already at the leave's stream position |
| Mode D = federation sender computed destinations from partial state 72 ms after the flag was cleared, so the leave was never sent | **High** — `Unexpectedly did not have cached prev group` + no outbound transaction + nothing on hs1 |
| Mode E = room missing from `sync_result_builder.joined_room_ids` | **Low-medium** — inference; needs instrumentation |
| Parallel subtests share users, so mode B/C/E attribution to a named subtest is noise | **High** — `federation_rooms_invite_test.go:28-31`, 8× `t.Parallel()` |
