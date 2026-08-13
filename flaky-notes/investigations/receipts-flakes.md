# `TestThreadedReceipts` and `TestThreadReceiptsInSyncMSC4102`

Two receipts-related Complement flakes, both almost exclusive to `(workers, Postgres)`.

| Test | Jobs | Branches | Arrangements | Tracking issue |
|---|---|---|---|---|
| `TestThreadedReceipts` | 23 | 18 | `workers, Postgres` ×22, `monolith, SQLite` ×1 | [#15517](https://github.com/element-hq/synapse/issues/15517) (empty stub) |
| `TestThreadReceiptsInSyncMSC4102` | 10 | 7 | `workers, Postgres` ×10 | [#19171](https://github.com/element-hq/synapse/issues/19171), dup [#19908](https://github.com/element-hq/synapse/issues/19908) |

Both live in `complement/tests/csapi/thread_notifications_test.go` but they are **two
completely unrelated bugs**. Do not treat them as one.

---

# Summary

**`TestThreadedReceipts` — Synapse bug, root cause found and reproduced deterministically.**
`event_push_summary` rows written by the 30-second `_rotate_notifs` background loop leave
`last_receipt_stream_ordering` as `NULL`. The reader
(`_get_unread_counts_by_pos_txn`) treats `NULL` as *"this summary already accounts for every
read receipt"* — a legacy-compat branch for rows written before schema delta `72/01`. So if a
rotation lands **after** a room's events are persisted but **before** the user sends a read
receipt for an *older* event, the receipt is silently ignored by the badge query until the
next rotation cycle (up to 30 s later). The test asserts the count 5 s after posting the
receipt, so it fails. Not workers-specific in mechanism; workers runs are just slower, which
widens the window (and 1/23 failures was indeed `monolith, SQLite`).

**`TestThreadReceiptsInSyncMSC4102` — different bug, already diagnosed upstream and stalled
on a spec disagreement.** MSC4102's "unthreaded receipt wins over a clashing threaded one"
is enforced only at *read* time in `ReceiptInRoom.merge_to_content`, which dedupes within a
single `/sync` response. When the two receipts land in different receipt-stream windows the
client's `/sync` only ever sees the threaded one (6/8 jobs) or neither (2/8). All 8 failures
are on the *federated* `hs2` side (line `:370`); the local side never fails. Erik Johnston's
[synapse#19838](https://github.com/element-hq/synapse/pull/19838) +
[complement#881](https://github.com/matrix-org/complement/pull/881) fix this, but
MadLittleMods has requested changes disputing the MSC reading. No new investigation needed
here — it needs a spec-interpretation decision, not more log-digging.

---

# `TestThreadedReceipts`

## Test shape

`complement/tests/csapi/thread_notifications_test.go:90-321`. Single test, no subtests.
Alice sends 7 events into one room:

```
A  m.room.message                     (main timeline, notifies bob)
B  m.thread -> A                      (thread A)
C  m.thread -> A, mentions bob        (thread A, HIGHLIGHT)
D  m.room.message, mentions bob       (main timeline, HIGHLIGHT)
E  m.thread -> A                      (thread A)
F  m.reference -> A                   (main timeline — a reference is NOT a thread)
G  m.annotation -> F  (reaction)      (no notification)
```

Bob's baseline: main = {A, D, F} = 3 notifs / 1 highlight; thread A = {B, C, E} = 3 notifs /
1 highlight. Unthreaded (unfiltered) `/sync` therefore reports `notification_count: 6,
highlight_count: 2`. The test then posts receipts and re-checks the counts:

| line | receipt posted | expected unfiltered counts |
|---|---|---|
| 192 | — | 6 / 2 |
| 217 → 218 | threaded `m.read` @ **A**, `thread_id: "main"` | **5** / 2 |
| 244 → 245 | threaded `m.read` @ **B**, `thread_id: A` | **4** / 2 |
| 272 → 273 | unthreaded `m.read` @ **D** | 2 / 0 |
| 300 → 301 | threaded `m.read` @ **G**, `thread_id: A` | 1 / 0 |

Both receipts that flake point at events that are **older** than the newest event in the
room. That is the trigger condition (see root cause).

## Evidence — 7 job logs, one failure mode

Failure is always `MustSyncUntil` timing out on the `syncHasUnreadNotifs` checker, on one of
the two *unfiltered* syncs (line 218 or 245). `SyncTimelineHas` and
`syncHasThreadedReadReceipt` always pass on response #1 (they are removed from the checker
slice, which is why only one checker's errors are printed) — **the receipt itself is
delivered correctly and promptly; only the notification count is wrong.**

| job | line | seen on response #1 | expected |
|---|---|---|---|
| `90690092998` | 218 | `{"notification_count":6,"highlight_count":2}` | 5 / 2 |
| `87212028700` | 218 | `{"notification_count":6,"highlight_count":2}` | 5 / 2 |
| `88341769166` | 218 | `{"notification_count":6,"highlight_count":2}` | 5 / 2 |
| `87696016306` | 218 | `{"notification_count":6,"highlight_count":2}` | 5 / 2 |
| `88362171804` | 245 | `{"notification_count":5,"highlight_count":2}` | 4 / 2 |
| `90662843237` | 245 | `{"notification_count":5,"highlight_count":2}` | 4 / 2 |
| `89464912225` | 245 | `{"notification_count":5,"highlight_count":2}` | 4 / 2 |

(`93011024778` is the 8th job id but its log is truncated before the gotestfmt group; only
the compact `FAIL TestThreadedReceipts 8.32s` line survives.)

Verbatim, from `data/joblogs/90690092998.log`:

```
thread_notifications_test.go:218: @user-173:hs1 MustSyncUntil: timed out after 5.03430485s. Seen 6 /sync responses. Checkers:
    [t=5.734067ms]   Response #1: syncHasUnreadNotifs(!KWFzrvfwRQFSgyTSbf:hs1): check function did not pass: {"notification_count":6,"highlight_count":2} /
    [t=1.01145656s]  Response #2: syncHasUnreadNotifs(!KWFzrvfwRQFSgyTSbf:hs1): missing unread notifications
    [t=2.017499371s] Response #3: syncHasUnreadNotifs(!KWFzrvfwRQFSgyTSbf:hs1): missing unread notifications
    ... (through Response #6)
```

Responses #2-6 say "missing unread notifications" because `MustSyncUntil` advances the since
token (`client/sync.go:118-120`) and Synapse only emits a joined-room entry — and hence
`unread_notifications` (`synapse/rest/client/sync.py:646`) — when the room has timeline
events, ephemeral, account data or state in the window
(`synapse/handlers/sync.py:3212-3219`). A count change alone never re-emits the room. So the
one wrong response is fatal; the test cannot converge.

## The smoking gun: `_rotate_notifs` immediately precedes the receipt in 7/7 jobs

Interleaving the homeserver container logs (`synapse_main | … Rotating notifications up
to: N`) with the `synapse.access.http` lines for the test's own room:

```
=== 90690092998 (fails at :218 with 6)
19:48:33,340  PUT  …/send/m.room.message      A
19:48:33,371  PUT  …/send/m.room.message      B
19:48:33,399  PUT  …/send/m.room.message      C
19:48:33,424  PUT  …/send/m.room.message      D
19:48:33,447  PUT  …/send/m.room.message      E
19:48:33,456  *** Rotating notifications up to: 1714   <-- lands here
19:48:33,484  PUT  …/send/m.room.message      F
19:48:33,509  PUT  …/send/m.room.reaction     G
19:48:33,538  POST …/receipt/m.read/$…A       (thread_id: main)

=== 88362171804 (fails at :245 with 5)
13:26:22,767  PUT  …/send/m.room.message      A
13:26:23,184  PUT  …/send/m.room.message      B
13:26:23,279  PUT  …/send/m.room.message      C
13:26:23,585  *** Rotating notifications up to: 1712   <-- lands here
13:26:23,650  PUT  …/send/m.room.message      D
13:26:23,744  PUT  …/send/m.room.message      E
13:26:24,119  PUT  …/send/m.room.message      F
13:26:24,210  PUT  …/send/m.room.reaction     G
13:26:24,589  POST …/receipt/m.read/$…A       (thread_id: main)   -> :218 PASSES
13:26:24,991  POST …/receipt/m.read/$…B       (thread_id: A)      -> :245 FAILS

=== 90662843237 (fails at :245 with 5) — identical shape to 88362171804
18:06:08,206/610/697  A, B, C
18:06:08,926  *** Rotating notifications up to: 1712
18:06:09,076/158/554/625  D, E, F, G
18:06:10,025  POST receipt @A main   -> :218 PASSES
18:06:10,429  POST receipt @B thread -> :245 FAILS
```

In **all seven** jobs a rotation cycle fires between 66 ms and 1.3 s before the first
receipt POST, and there is no rotation between the receipt and the failing assertion. The
rotation period is 30 s, so this is not coincidence.

Better: *where* the rotation lands predicts *which* assertion fails and *what number* is
reported (arithmetic in the next section):

- rotation lands after E (covers A–E, misses F/G) → line 218 reports **6**
- rotation lands after C (covers A–C, misses D–G) → line 218 passes, line 245 reports **5**

Both predictions match the logs exactly.

## Root cause (confirmed, and reproduced deterministically)

### The mechanism

`event_push_summary` is maintained entirely by the `_rotate_notifs` background loop
(`synapse/storage/databases/main/event_push_actions.py:1372`, run every 30 s on the process
with `run_background_tasks`). It has two phases:

1. `_handle_new_receipts_for_notifs_txn` (`:1405`) — for each *new* receipt, delete the
   now-read push actions and rewrite the affected summary rows, **stamping
   `last_receipt_stream_ordering`**.
2. `_rotate_notifs_before_txn` (`:1642`) — fold newly-persisted `event_push_actions` into
   `event_push_summary`. Its upsert is

   ```python
   value_names=("notif_count", "unread_count", "stream_ordering"),
   ```

   — `last_receipt_stream_ordering` is **not** in the list, so a freshly INSERTed row gets
   the column default, `NULL` (`schema/main/delta/72/01event_push_summary_receipt.sql:42`,
   nullable `BIGINT`, no default).

Between rotations, the reader `_get_unread_counts_by_pos_txn` (`:559`) is responsible for
noticing that a summary row predates a receipt. Its guard (`:614-644`):

```sql
WHERE room_id = ? AND user_id = ?
AND (
    (last_receipt_stream_ordering IS NULL AND stream_ordering > COALESCE(threaded_receipt_stream_ordering, ?))
    OR last_receipt_stream_ordering = COALESCE(threaded_receipt_stream_ordering, ?)
) AND (notif_count != 0 OR COALESCE(unread_count, 0) != 0)
```

with the accompanying comment:

> If `last_receipt_stream_ordering` is null then that means it's up-to-date (as the row was
> written by an older version of Synapse that updated `event_push_summary` synchronously
> when persisting a new read receipt).

**That premise is false today.** `_rotate_notifs_before_txn` still produces `NULL` rows
constantly — every room's first rotation before its first receipt. And the `NULL` branch only
checks that the summary's *upper* bound (`stream_ordering`) is past the receipt; it never
checks the summary's *lower* bound. A rotation-written row counts from the beginning of the
room, so it happily includes events at or below the receipt.

Concretely: summary row `('main', notif_count=3, stream_ordering=SO(F), last_receipt=NULL)`,
receipt at `SO(A)`. `SO(F) > SO(A)` → branch 1 matches → the pre-receipt count of 3 is used
verbatim. The receipt is a complete no-op for the badge.

Once the summary row is accepted the read path never re-checks: the highlight query (`:653`)
and the "top up with un-rotated actions" query (`:700`,
`_get_notif_unread_count_for_user_room` at `:769`) only add actions with `stream_ordering >
rotated_upto_stream_ordering` — no receipt filter, because the summary was supposed to have
handled that.

### Arithmetic check against the logs

Let `a<b<c<d<e<f<g` be the stream orderings of A…G, `R` the rotation high-water mark.

*Rotation after E (`R = e`), receipt `main@a`:*
summary `main` = {A, D} = 2, `stream_ordering = d`; `d > a` → accepted → 2.
summary thread A = {B, C, E} = 3, accepted → 3.
Top-up above `R`: F (main, notif) → main = 3. Highlights: D and C → 2.
**Total 3 + 3 = 6 notifs / 2 highlights.** Matches `90690092998`, `87212028700`,
`88341769166`, `87696016306`.

*Rotation after C (`R = c`), receipt 1 `main@a`:*
summary `main` = {A} = 1, `stream_ordering = a`; `a > a` is false → **rejected** → main
counted live above `a` = {D, F} = 2. ✔
summary thread A = {B, C} = 2, `c > join` → accepted; top-up above `c` adds E → 3. ✔
**Total 5** → line 218 passes.

*Same `R`, receipt 2 `B @ thread A`:*
summary thread A: `last_receipt IS NULL AND c > b` → accepted with count **2 — which still
includes B, the event just marked read**; top-up adds E → 3 instead of 2.
main = 2 (live). **Total 5, expected 4.** Matches `88362171804`, `90662843237`,
`89464912225`.

### Deterministic reproduction

Reproduced on a plain **monolith + SQLite** `HomeserverTestCase` — no workers, no Postgres,
no timing:

```
BEFORE ROTATE: NotifCounts(notify_count=2, unread_count=0, highlight_count=1)
AFTER ROTATE : NotifCounts(notify_count=2, unread_count=0, highlight_count=1)
SUMMARY ROWS : [('$…threadA', 2, 10, None), ('main', 2, 11, None)]
                                                          ^^^^ last_receipt_stream_ordering
AFTER RECEIPT: NotifCounts(notify_count=2, unread_count=0, highlight_count=1)   <-- WRONG, should be 1
AFTER 2nd ROT: NotifCounts(notify_count=1, unread_count=0, highlight_count=1)   <-- repaired
```

Control with **no** rotation before the receipt gives the correct `notify_count=1`.

Recipe (matches the Complement test): send A (main), B + C (thread A), D (main highlight);
call `store._rotate_notifs()`; post `insert_receipt(..., event_ids=[A], thread_id="main")`;
read `store._get_unread_counts_by_receipt_txn`. Scratch copy at
`<scratchpad>/repro/test_rot.py`.

Why the existing unit tests miss it: `tests/storage/test_event_push_actions.py` always calls
its `_rotate()` helper *after* `_mark_read()`, and `_rotate_notifs()` runs
`_handle_new_receipts_for_notifs_txn` first — so every summary row in those tests already has
a non-`NULL` `last_receipt_stream_ordering` by the time it is read. The two tests added by
#19785 (`test_count_aggregation_receipt_before_first_rotation[_in_thread]`) cover the
*opposite* ordering (receipt → rotation). Nothing covers rotation → receipt-for-an-older-event.

### Why `(workers, Postgres)`

Nothing in the mechanism is worker- or Postgres-specific, and 1 of the 23 failures was
`monolith, SQLite`, which supports rather than contradicts this. The bias is window width:
the vulnerable window is "a `_rotate_notifs` tick lands between the last event send and the
receipt POST". In the failing worker runs that gap is ~0.3–1.8 s of wall clock (7
`SendEventSynced` round trips plus two `MustSyncUntil` loops), against a 30 s rotation
period. Worker deployments are slower per request and the containers are long-lived (in
`90690092998` the homeserver had been up 90 s when the test ran, so three rotations had
already fired), so both the window and the number of rotation opportunities are larger.

### Relation to #19785

`6d289f7ce0` "Fix permanent badge inflation from read receipts before first rotation
(#19785)" (Stefan Ceriu, 2026-06-23) patched the *mirror image* of this bug — a receipt
arriving before any summary row exists, where `_handle_new_receipts_for_notifs_txn`'s
`UPDATE` was a silent no-op and rotation then INSERTed with `last_receipt_stream_ordering =
NULL`. Its in-code comments (`:1512-1520`, `:1556-1559`) describe exactly this hazard class.
It does not touch `_rotate_notifs_before_txn`, so rotation-first still produces `NULL` rows.
Failing jobs here are Synapse 1.157.2+ (2026-07/08), i.e. after that fix.

## Recommended fix — Synapse side

`synapse/storage/databases/main/event_push_actions.py`. Two options; **(A) is the one I'd
ship**, (B) is the belt-and-braces companion.

**(A) Stop trusting `NULL` blindly in `_get_unread_counts_by_pos_txn` (`:559`).**
A `NULL` row counts from the user's join, so it is only sound when the user has no read
receipt in the room at all. `_get_unread_counts_by_receipt_txn` (`:525`) already knows this —
it branches on whether `get_last_unthreaded_receipt_for_user_txn` returned a row. Thread that
boolean (say `has_unthreaded_receipt: bool`) into `_get_unread_counts_by_pos_txn` and change
the guard to:

```sql
AND (
    (last_receipt_stream_ordering IS NULL
        AND threaded_receipt_stream_ordering IS NULL
        AND NOT ?                      -- has_unthreaded_receipt
        AND stream_ordering > COALESCE(threaded_receipt_stream_ordering, ?))
    OR last_receipt_stream_ordering = COALESCE(threaded_receipt_stream_ordering, ?)
)
```

When the row is rejected the query already falls through to the live `event_push_actions`
count (`:727-761`), which is correct. Reviewer must check the interaction with
`_remove_old_push_actions_that_have_rotated` (`:1747`): push actions that were rotated *and*
are >1 day old are deleted, so a live fallback can undercount. That needs a `NULL` summary row
surviving >1 day, which requires the room to have had zero receipts for >1 day — then the
first receipt causes at most a 30 s undercount before the next rotation stamps the row. A
transient undercount is strictly better than the current transient overcount, but say so in
the PR.

**(B) Stop generating `NULL` rows in `_rotate_notifs_before_txn` (`:1642`).**
Add `last_receipt_stream_ordering` to the upsert's `value_names`, computed per
`(user_id, room_id, thread_id)` as the user's current max receipt `event_stream_ordering` for
that thread, falling back to the unthreaded receipt and then to the join event — i.e. exactly
`COALESCE(threaded_receipt_stream_ordering, unthreaded_receipt_stream_ordering)` as the reader
computes it, so branch 2 matches. This is the "correct" fix but the value must be *exactly*
right: get it wrong and rows are permanently rejected, which silently loses counts for actions
already deleted by `_remove_old_push_actions_that_have_rotated`. Note the existing upsert
deliberately omits the column so that it is *preserved* on conflict — only newly INSERTed rows
need a value, so `COALESCE(old.last_receipt_stream_ordering, <computed>)`.

**Regression test** (this is the important deliverable — it is fully deterministic):
add to `tests/storage/test_event_push_actions.py`, next to
`test_count_aggregation_receipt_before_first_rotation`:

```python
def test_count_aggregation_receipt_for_old_event_after_rotation(self) -> None:
    """A receipt for an event older than the summary high-water mark must be
    honoured immediately, not only after the next rotation."""
    # A (main), B + C (thread A), D (main, highlight)
    # _rotate()
    # _mark_read(A, MAIN_TIMELINE)
    # _assert_counts(main=1, ...)   # currently 2
```

**No Complement change is needed for this test.** The assertions are correct and a
spec-compliant server passes them: Synapse *does* wake `/sync` on the receipt and *does*
include the room in that response, so the correct count is deliverable in response #1.
`MustSyncUntil`'s advancing since-token makes the failure hard rather than eventually-green,
which is the desired behaviour here.

**Confidence: very high (~95%).** Mechanism proven by deterministic local reproduction plus
the `event_push_summary` row dump; the failure timing and both distinct wrong values (6 and
5) are predicted from independently-observed rotation timestamps in 7/7 job logs. The
residual 5% is only about which of (A)/(B) upstream prefers.

---

# `TestThreadReceiptsInSyncMSC4102`

## Test shape

`complement/tests/csapi/thread_notifications_test.go:325-376`, 2-server deployment.
Alice on `hs1`, Bob on `hs2` joined over federation. Alice sends A, then B (`m.thread` → A),
then posts **two receipts for the same event B back to back**:

```go
alice.MustDo(t, "POST", …/receipt/m.read/eventB, client.WithJSONBody(t, struct{}{}))                      // unthreaded
alice.MustDo(t, "POST", …/receipt/m.read/eventB, client.WithJSONBody(t, map[string]any{"thread_id": eventA})) // threaded
alice.MustSyncUntil(t, client.SyncReq{}, syncHasUnthreadedReadReceipt(roomID, alice.UserID, eventB)) // :362 local
bob.MustSyncUntil(t,   client.SyncReq{}, syncHasUnthreadedReadReceipt(roomID, alice.UserID, eventB)) // :370 federated
```

`syncHasUnthreadedReadReceipt` (`:37-42`) requires a receipt for alice at B with **no**
`thread_id` key.

## Evidence — 8 job logs, always line `:370`

`88694821018 92330586006 90648776237 91245127279 93537721578 91753362199 90940667245
92331381436`. **Every one fails at line 370** — bob's *federated* sync on `hs2`. Line 362
(alice's own sync on `hs1`) never fails: the local side always sees the unthreaded receipt.
Timeouts are 5.6–5.7 s over 8 responses (one job, 10).

Two manifestations of the same loss:

| variant | jobs | what bob's `ephemeral.events` contains |
|---|---|---|
| **A** — threaded receipt served instead of unthreaded | 6/8 (`92330586006`, `90648776237`, `91245127279`, `91753362199`, `90940667245`, `92331381436`) | exactly 1 element, the receipt for eventB carrying `thread_id: <eventA>` |
| **B** — nothing served at all | 2/8 (`88694821018`, `93537721578`) | `ephemeral.events` present but **empty**, in two consecutive responses |

Variant A, `92330586006`:

```
[t=668.152236ms] Response #3: SyncEphemeralHas(!VKYJVmFJpIgtADGUfe:hs1): check function did not pass while iterating over 1 elements:
    [{"type":"m.receipt","content":{"$xP7ntAwr_V2Kw0aBlirLeE-FDS-sDp7fzU91QNia_xI":{"m.read":{"@user-175:hs1":{"thread_id":"$tL63ro1szCemgKDfTcdNlLKy4MGixf_Y53rvNWm5V84","ts":1785939285264}}}}}]
```

Variant B, `88694821018`:

```
[t=660.166473ms] Response #2: SyncEphemeralHas(!yiRMTOEPxbofdWpXtm:hs1): check function did not pass while iterating over 0 elements: []
[t=672.973888ms] Response #3: SyncEphemeralHas(!yiRMTOEPxbofdWpXtm:hs1): check function did not pass while iterating over 0 elements: []
```

In both variants every later response reverts to `Key rooms.join.<roomID>.ephemeral.events
does not exist` — the room drops out of incremental `/sync` once its ephemeral delta has been
consumed, so the receipt is never re-offered and the test cannot converge.

**The EDUs always arrive.** Container-log deep dive on `88694821018`, `93537721578` and
`92330586006` shows identical routing in all three: both `POST …/receipt/m.read/<eventB>` on
`hs1` are handled by `stream_writers1` (the receipts stream writer); `federation_sender1`
dispatches them; on `hs2` they arrive via `federation_inbound1` → `master` → `stream_writers1`
(`Got 'm.receipt' edu from hs1`, twice), and `hs2`'s `stream_writers1` logs two consecutive
`Sending update for receipts: 1 -> 2` then `2 -> 3`. `/sync` on both servers is served by
`synchrotron1`. So this is **not** federation delivery loss — both receipts are always
delivered and both advance `hs2`'s receipts stream. The loss is in how `hs2`'s `/sync`
turns those two stream positions into ephemeral events.

(`93537721578` additionally shows an unrelated partial-state race delaying eventB's
persistence — `postgres | ERROR: insert or update on table "partial_state_events" violates
foreign key constraint` / `event_persister1 | Room … was un-partial stated while processing
the PDU, trying again` — which is probably why that run landed in variant B rather than A.)

The same assertion appears in issue #19171 (MadLittleMods, 2025-11-12):

```
thread_notifications_test.go:370: @user-167:hs2 MustSyncUntil: timed out after 6.004202564s. Seen 10 /sync responses. Checkers:
    [t=151.041456ms] Response #1: SyncEphemeralHas(!rkvKOcpUehaYxuerxr:hs1): Key rooms.join.!rkvKOcpUehaYxuerxr:hs1.ephemeral.events does not exist
    [t=603.097719ms] Response #3: SyncEphemeralHas(!rkvKOcpUehaYxuerxr:hs1): check function did not pass while iterating over 1 elements:
        [{"type":"m.receipt","content":{"$ybQ1MPrWa-s5Equ-NktU3LMrqUIpE1Nej46GzbkrFIY":{"m.read":{"@user-166:hs1":{"thread_id":"$chL6ViiGN82z1AG_HYNpBHu5Kz1d7gitfQcgjatNM4o","ts":1762815095353}}}}}]
```

From #19908 (gamesguru, 2026-07-03), structurally identical, also `:370`, Synapse
`ab277b3e3f`, Postgres, multiple workers:

```
thread_notifications_test.go:370: @user-175:hs2 MustSyncUntil: timed out after 5.699962045s. Seen 9 /sync responses. Checkers:
    [t=28.3491ms]     Response #1: SyncEphemeralHas(!qFrSxZPtgSblQeZLrG:hs1): Key rooms.join.!qFrSxZPtgSblQeZLrG:hs1.ephemeral.events does not exist
    [t=658.056409ms]  Response #3: SyncEphemeralHas(!qFrSxZPtgSblQeZLrG:hs1): check function did not pass while iterating over 1 elements:
        [{"type":"m.receipt","content":{"$JDQ_oohdm0NDfd2Ea2xTuzAJDmV0GSfWMzTcAotEqKI":{"m.read":{"@user-174:hs1":{"thread_id":"$UGkG73htT58nkjg7DMuxlOTbvWUd6JRAPYETSsQfqvI","ts":1783080803190}}}}}]
```

Always line `:370` (the remote `hs2` side), always the same assertion.

## Mechanism

`ReceiptInRoom.merge_to_content` (`synapse/storage/databases/main/receipts.py:73-112`) is the
only place MSC4102's "unthreaded wins" is applied:

```python
unthreaded_receipts: set[tuple[str, str]] = {
    (receipt.user_id, receipt.event_id)
    for receipt in receipts
    if receipt.thread_id is None
}
...
    if receipt.thread_id is not None:
        if (receipt.user_id, receipt.event_id) in unthreaded_receipts:
            # Ignore threaded receipts if we have an unthreaded one.
            continue
```

`receipts` here is only the receipts pulled for **this** `/sync` window (`:500`, inside
`_get_linearized_receipts_for_rooms`). The two receipts are separate rows in
`receipts_linearized` (the unique key includes `thread_id`, and
`_insert_linearized_receipt_txn` only supersedes a receipt with the *same* `thread_id`), at
stream positions 2 and 3. If a sync window covers only position 3, `unthreaded_receipts` is
empty and the threaded receipt is emitted unchallenged → **variant A**. If a window covers
both, the dedup works and the test passes — which is why this is a flake and not a permanent
failure.

Variant B is the sharper problem: `hs2`'s stream advanced through both positions while the
room's ephemeral output was empty, i.e. the receipts were consumed by windows that emitted
nothing for them. Nothing about MSC4102 explains that; it is plain data loss in the receipts
sync source.

## Root cause (upstream diagnosis, from PR #19838)

> MSC4102 requires that an unthreaded read receipt always wins over a clashing threaded one
> (same user, same event). Today that's enforced **only at read time**, in
> `ReceiptInRoom.merge_to_content`, which dedupes a clashing pair *within a single `/sync`
> response*.
>
> That breaks down when the two receipts are persisted at different stream positions and get
> served in **separate** `/sync` responses. Tracing the failing run:
>
> 1. Alice sends an unthreaded receipt for event B, then a threaded receipt for the same event
>    B. Both are federated to hs2 as two separate `m.receipt` EDUs and persisted there at
>    receipts stream positions 2 (unthreaded) and 3 (threaded).
> 2. Bob's initial sync advanced its receipt token to 2 but emitted no receipt; his next
>    (incremental) sync window was `(2,3]`, containing only the threaded receipt.
> 3. The unthreaded receipt was never surfaced, so the threaded one "won" → MSC4102 violation
>    → timeout.

This is consistent with the logs above (Response #1 has no ephemeral at all — the token was
advanced past the unthreaded receipt without emitting it; Response #3 carries only the
threaded one).

Classification: **(a) Synapse bug**, with an (c) spec-ambiguity overlay. The underlying spec
problem is [matrix-spec#1727](https://github.com/matrix-org/matrix-spec/issues/1727): the
`m.receipt` EDU shape is keyed on `(event_id, user_id)` and simply cannot express a user
having both an unthreaded and a threaded receipt on the same event, so one must be dropped in
an undefined way.

The "also reproduces on non-Synapse homeservers" claim in #19908 is **weak**: it is a single
unelaborated sentence ("Also observed in Rust servers, so I'm unclear whether this is a
Complement or homeserver issue"), names no implementation, and the attached log is from
Synapse. It should not be read as evidence of a Complement-side race.

## Existing (stalled) fixes

- [synapse#19838](https://github.com/element-hq/synapse/pull/19838) — "Make MSC4102 'prefer
  unthreaded receipt' durable at insert time". In `_insert_linearized_receipt_txn`
  (`synapse/storage/databases/main/receipts.py`), drop a threaded receipt if an unthreaded
  receipt for the same `(room, type, user)` already exists for the same event.
  **`CHANGES_REQUESTED`.**
- [complement#881](https://github.com/matrix-org/complement/pull/881) — make the test
  deterministic by waiting for each user to observe the unthreaded receipt before sending the
  clashing threaded one, then asserting the threaded one never wins. **Open.**

Blocking disagreement (MadLittleMods on #881):

> [MSC4102] only mentions that the de-duplication should happen when assembling EDU's and
> there is a unthreaded and threaded receipt in the same response. It doesn't say anything
> about an unthreaded read receipt winning out over new threaded read receipts.
>
> […] it's okay to send and receive a threaded read receipt for the same event two hours
> after the unthreaded read receipt.

and on the trace above:

> If this breakdown is to be believed, the bug appears to be in the following behavior
> especially "The unthreaded receipt was never surfaced" part. **Why is `/sync` advancing past
> its persisted position?**

That last question is the sharpest one and, in my reading, the real bug regardless of the MSC
interpretation: a `/sync` that advances its receipt token past position 2 while emitting
nothing for position 2 is dropping data. Worth checking
`ReceiptEventSource.get_new_events` / the `_receipts_stream_cache` bounds on the `hs2` side
independently of the "unthreaded wins" argument.

## Recommendation

No new Complement or Synapse work should start here until the MSC4102 reading is settled —
it is a decision, not an investigation. Concretely:

1. Get a ruling on whether "unthreaded wins" is a property of EDU assembly only (MadLittleMods)
   or of the receipt store (Erik). MSC4102's text supports MadLittleMods; the practical
   consequence of matrix-spec#1727 supports Erik.
2. Independently of (1), chase MadLittleMods's question: on `hs2`, establish whether the
   unthreaded receipt at stream position 2 was skipped because of read-time dedup or because
   the sync token genuinely advanced past an unemitted position. If the latter, that is a
   plain data-loss bug in the receipts sync source and should be fixed on its own.
3. Land complement#881's determinism change (wait for the unthreaded receipt to be observed
   before sending the threaded one) regardless of (1) — it converts a flake into a stable
   pass/fail and costs nothing. Complement checkout:
   `/Users/quenting/Documents/matrix/complement`.

**Confidence: high (~85%)** for variant A (6/8 jobs): #19838's trace matches the code in
`merge_to_content` and the container logs independently confirm its two load-bearing claims —
both EDUs are delivered, and `hs2`'s receipts stream advances `1 -> 2 -> 3` as two separate
positions. #19838's *fix shape* (drop the threaded receipt at insert time) is a different
question and is what the review is contesting.

**Confidence: low (~40%)** that #19838 also fixes variant B (2/8 jobs), where bob's ephemeral
array is empty in both windows. Dropping the threaded receipt at insert time would leave the
unthreaded one — but the unthreaded one is exactly what is already being lost in variant B, so
the fix may not help there. Step (2) above is aimed at this.

## Issue reconciliation

- **#15517** (`TestThreadedReceipts`) — open, but a dead stub: auto-migrated by `matrixbot`
  from `matrix-org/synapse#15517`, body is one dead CI link, zero comments in two years, no
  assertion text and no hypothesis. Nothing to reconcile; my findings are the first analysis.
  Worth posting the root cause there and relabelling.
- **#19171** — open, correctly scoped, and its "the Complement test itself looks pretty sound
  and this is probably a Synapse bug" call matches my reading. MadLittleMods could not
  reproduce locally in 2k+ `WORKERS=1` runs, which fits a narrow federation-timing window.
- **#19908** — closed as a duplicate of #19171 by MadLittleMods, correctly. Its "Rust servers"
  aside is unsubstantiated (see above) and should not redirect the investigation
  Complement-ward.
- Both tests are also listed as unchecked entries on the meta-issue
  [#18537](https://github.com/element-hq/synapse/issues/18537).

---

# Implementation (`TestThreadedReceipts`)

**Shipped fix: writer + bounded reader.** The first attempt was option (A) alone (reader-side:
"user has a receipt ⇒ never trust a NULL `last_receipt_stream_ordering`"). Review **blocked**
it with a verified counter-example, and the counter-example is now a regression test.

## The counter-example that killed reader-only

`_rotate_notifs_before_txn` mints a summary row for every thread with new push actions —
including a thread that first appears **after** the user's last receipt was processed. Its
upsert omitted `last_receipt_stream_ordering`, so the row was NULL, and
`_handle_new_receipts_for_notifs_txn` only ever stamps rows in response to a *new* receipt, so
it stayed NULL forever. An unconditional reader-side guard therefore rejected it forever, and
once `_remove_old_push_actions_that_have_rotated` deleted the backing push actions (>1 day) the
fallback recount returned 0: **notifications permanently vanish**, rather than the old ≤30 s
overcount. The investigation above bounded this hazard as "room had zero receipts for >1 day";
the real condition is just "summary row minted after the last processed receipt", which is
common.

Note also that option (B) alone does **not** fix the flake: in the failing scenario there
genuinely is no receipt when the row is minted, so any correct stamp is "no receipt". The row
only goes stale later, when the receipt arrives. Both halves are needed.

## `synapse/storage/databases/main/event_push_actions.py`

1. **Writer.** `_rotate_notifs_before_txn` now selects
   `COALESCE(old.last_receipt_stream_ordering, <max receipt for (user, room, thread)>)` (a
   correlated `receipts_linearized` lookup matching `thread_id` or an unthreaded receipt) and
   includes `last_receipt_stream_ordering` in the upsert. `_rotate_notifs` runs
   `_handle_new_receipts_for_notifs_txn` to completion first, so that max receipt is exactly
   what the counts are relative to. `COALESCE` on the *old* value means an existing row keeps
   the stamp `_handle_new_receipts_for_notifs_txn` gave it — the rotation never claims to
   account for a receipt it didn't recalculate against (this is what keeps #19785's surviving
   below-receipt highlights from being double counted). A row stays NULL only when the user
   has never sent a receipt in the room, which is exactly when the reader's NULL branch is
   sound. `_EventPushSummary` gained the field.
2. **Reader.** `_get_unread_counts_by_pos_txn`'s NULL branch gains one conjunct:
   `COALESCE(threaded_receipt_stream_ordering, <unthreaded receipt or 0>) <= <deletion
   horizon>`. With no receipt at all this is `0 <= horizon` → the branch is preserved
   unchanged. With a receipt it distrusts the row **only** while the manual recount can still
   see the actions it summarised. The horizon is `stream_ordering_day_ago` (new
   `_pruned_upto_stream_ordering()` helper; `sys.maxsize`, i.e. trust everything, if it isn't
   known yet), because `_remove_old_push_actions_that_have_rotated` only ever deletes below
   it. Below the horizon we keep serving the (possibly stale) summary and let the next
   rotation correct it, exactly as before. The bound is a heuristic rather than a proof:
   `stream_ordering_day_ago` is per-process and refreshed every ten minutes while the deleter
   may run on another worker, so it can lag by about that much. The exposure if it does is the
   same bounded, self-healing staleness accepted everywhere else here, not loss.
3. **Reader, unreconcilable stamps.** Re-review found the horizon bound only covered the
   NULL branch: a row with a *non-NULL* stamp that no longer matches was still rejected
   unconditionally, and could be rejected *forever*. `from_stream_ordering` falls back to the
   user's membership event when they have no unthreaded receipt, so a row stamped with a
   receipt at or before that join can never match — e.g. the user read back into history they
   could already see, or left and rejoined. Nothing recalculates such a row either (the
   receipt is not new, so `_handle_new_receipts_for_notifs_txn` never revisits it), so once
   the push actions are pruned the counts are gone for good. When the user has no unthreaded
   receipt and the thread has no threaded receipt above the join, a stamped row is now kept.

   The reviewer's literal suggestion — bound the *mismatch* branch by the same pruning
   horizon — was tried and **rejected empirically**: it breaks three tests, including both
   flake regression tests. Trusting any row whose comparison position sits below the horizon
   means a receipt for an event more than a day old stops clearing the badge immediately,
   which is the exact symptom this PR exists to fix. The distinction that matters is not the
   horizon but whether there is a *newer receipt* to reconcile against: if there is, rejecting
   the row matches what the next rotation will compute anyway (it also recounts from
   `event_push_actions`), so nothing is lost that Synapse would not have lost regardless; if
   there is not, nothing will ever recalculate the row and rejecting it is pure loss. Hence
   the narrow clause rather than a uniform horizon. The badge path needs no equivalent: it
   compares against the max receipt directly and never falls back to the join.
4. `_get_unread_counts_by_room_for_user_txn` (push-badge path) had the identical NULL flaw and
   gets the same `<= horizon` conjunct.
5. Same function: the receipt max clause now `COALESCE(..., 0)`s **both** columns on Postgres
   too. Previously `GREATEST(NULL, NULL)` was NULL there, so `stream_ordering > NULL` was
   unknown and the NULL-trusting branch was unreachable on Postgres only — meaning a
   no-receipt room whose actions had been pruned already lost its badge on Postgres. The two
   engines now agree.
6. Same function: `seen_thread_ids` was a **global** set of thread IDs, so the "recheck
   `event_push_actions` for anything without a valid summary" query excluded `main` in *every*
   room as soon as any one room had a valid `main` summary. Now keyed on
   `(room_id, thread_id)`, with the final query grouping by `room_id, thread_id` and filtering
   in Python instead of with a `NOT IN` clause.
7. Signature: `_get_unread_counts_by_pos_txn` now takes a single
   `unthreaded_receipt_stream_ordering: int | None` and does the `local_current_membership`
   join lookup itself when it is `None`, instead of an `int` + `bool` pair that could
   disagree. `_get_unread_counts_by_receipt_txn` shrinks to a receipt lookup and a call.
8. Drive-by (review-requested): the "top up summarised threads with un-rotated actions" loop
   incremented the loop variable `counts` leaked from the summary loop for `MAIN_TIMELINE`,
   which would attribute main-timeline top-ups to whichever thread was summarised last. Now
   uniformly `_get_thread(thread_id)`. Pinned by a test that inserts a synthetic summary row
   with a thread ID sorting *after* `main`, rather than relying on the row order that masks
   the bug in practice (`event_push_summary`'s unique index is `(user_id, room_id, thread_id)`
   and real thread IDs are event IDs starting with `$`, so `main` normally comes last).
9. Comments: `_pruned_upto_stream_ordering()` now says it is a heuristic — the value is
   per-process and refreshed every ten minutes while the deleter may be on another worker, so
   it can lag; the cost of being wrong is the same bounded staleness accepted elsewhere. The
   `sys.maxsize` branch is documented as not happening in practice
   (`stream_ordering_day_ago` is populated synchronously in `__init__`). The writer comment no
   longer claims `_handle_new_receipts_for_notifs_txn` has seen every receipt — the phases are
   separate transactions, and a receipt landing in the gap self-heals on the next rotation
   after overcounting by one until then (reproduces identically on develop). A NOTE records
   that `_handle_new_receipts_for_notifs_txn`'s receipt select has no `receipt_type` filter
   while the writer and readers filter to `(READ, READ_PRIVATE)`; pre-existing, and with (3)
   in place the consequence is a bounded overcount rather than loss.

No backfill or schema change: pre-existing NULL rows are handled by (2)'s horizon bound, which
is what makes reader-side logic safe for them without a migration.

## Tests — `tests/storage/test_event_push_actions.py`

Five new deterministic tests (plus a shared `_send_message` helper); the first two rotate
**before** the receipt, which is what the existing tests never do:

- `test_count_aggregation_threaded_receipt_for_old_event_after_rotation` — the Complement
  recipe. Reverting the reader change: `notify_count=2` instead of `1` (main), then `2`
  instead of `1` (thread) — the two wrong values seen in CI.
- `test_count_aggregation_unthreaded_receipt_for_old_event_after_rotation` — the unthreaded
  path, with a **second room** whose summary is still valid so that item (5) is covered.
  Reverting item (5) alone: aggregate badge `0` instead of `1`.
- `test_count_aggregation_receipt_before_join_survives_pruning` — item (3): threaded receipt,
  rotate, leave + rejoin so the membership event outranks the receipt, prune. Without the
  clause the count drops from 1 to 0 permanently.
- `test_count_aggregation_main_timeline_top_up_stays_on_main_timeline` — item (8).
- `test_count_aggregation_summary_written_after_receipt_survives_pruning` — the reviewer's
  counter-example: processed receipt → rotate → new thread with two notifying messages →
  rotate → +1 day → `_remove_old_push_actions_that_have_rotated()`. Asserts both that every
  rotation-written row has a non-NULL `last_receipt_stream_ordering` and that the counts
  survive with no `event_push_actions` rows left.

Verified against a faithful simulation of the rejected reader-only patch (rotation upsert
omitting the column + horizon forced to 0): the new test fails with `{}` instead of
`{thread: 2}`. Verified that the writer fix **alone** (horizon forced to 0) already makes it
pass, i.e. the two halves are independently sufficient here.

## Results

- SQLite and `SYNAPSE_POSTGRES=1`, both green: `tests.storage.test_event_push_actions` (14),
  `tests.push`, `tests.replication.test_sharded_event_persister`, `tests.handlers.test_receipts`,
  `tests.storage.databases.main.test_receipts` — 116 tests (2 Postgres-only skips on SQLite).
- `ruff format` / `ruff check` / `mypy` clean on both changed files.
- Changelog: `changelog.d/15517.bugfix`.

## Residual risk

A receipt pointing at an event older than a day still takes up to one rotation cycle to be
reflected, by design (that is the price of never losing a pruned count). The correlated
receipt lookup in `_rotate_notifs_before_txn` runs once per rotated `(user, room, thread)`
row, served by `receipts_linearized_uniqueness_thread`; rotation batches are bounded by
`_rotate_count`.
