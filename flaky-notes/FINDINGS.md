# Complement/trial CI flakiness — findings (2026-08-13)

Data: last 400 runs of `tests.yml` on element-hq/synapse (≈2026-07-14 → 08-13),
all branches/events, per-job conclusions across all retry attempts, plus full
logs of every failed complement/trial job. Scripts in `scripts/`, raw data in
`data/` (gitignored), aggregation in `data/report.md` / `data/summary.json`.

## Headline numbers

- `complement / complement (workers, Postgres)`: **142 failures vs 101
  successes** among completed jobs — a **58% failure rate**. The two monolith
  arrangements fail an order of magnitude less often.
- **50% of develop-push runs** (30/60) had ≥1 complement job failure. develop
  is effectively expected-red.
- 26 failed jobs were retry-confirmed flakes (same job green on re-run attempt);
  **all 26 were complement jobs**, none trial.

## Complement: it's basically 5 tests

Of the 142 failed (workers, Postgres) jobs, **120 (85%) failed *only* on tests
from the top-5 flake families** below; 64 (45%) failed *only* on the single
top test. Ranked by failed-job count (branches/days spread confirms flake, not
broken code):

| fails | branches | days | test | tracking issue |
|---|---|---|---|---|
| 87 | 38 | 22 | `TestRoomSummaryAllowedRoomIDs/restricted_room_includes_allowed_room_ids` | [#19905](https://github.com/element-hq/synapse/issues/19905) — diagnosed: `join_rule` read from async-populated `room_stats_state`; fix proposed |
| 23 | 18 | 16 | `TestThreadedReceipts` | [#15517](https://github.com/element-hq/synapse/issues/15517) — **diagnosed + fixed in this workspace**: rotation-then-receipt race — `_rotate_notifs` writes `event_push_summary` rows with NULL `last_receipt_stream_ordering`, reader treats NULL as "receipt accounted for", so receipts for events older than the rotation high-water mark are no-ops for ≤30s. Mirror image of #19785. See [`investigations/receipts-flakes.md`](investigations/receipts-flakes.md) |
| 18 | 7 | 11 | `TestFederationRoomsInvite/*` (4 subtests) | [#19858](https://github.com/element-hq/synapse/issues/19858) — **diagnosed: 5 distinct real Synapse bugs**, 4 unreported (403 on `/leave`; `/sync` 500 = #19858, fix open in [#19960](https://github.com/element-hq/synapse/pull/19960); out-of-band leave invisible to initial `/sync`; leave never federated; + a repo-wide `@cachedList` exception-swallowing bug). See [`investigations/federation-invite-flakes.md`](investigations/federation-invite-flakes.md) |
| 12 | 10 | 9 | `TestMessagesOverFederation/Backfill_from_nearby_backward_extremities_past_token` | [#19907](https://github.com/element-hq/synapse/issues/19907), deep analysis in [complement#887](https://github.com/matrix-org/complement/issues/887) |
| 10 | 7 | 8 | `TestThreadReceiptsInSyncMSC4102` | [#19171](https://github.com/element-hq/synapse/issues/19171) — cross-window receipt merge on the federated HS; fix exists ([#19838](https://github.com/element-hq/synapse/pull/19838) + [complement#881](https://github.com/matrix-org/complement/pull/881)) but stalled on an MSC4102 interpretation dispute. See [`investigations/receipts-flakes.md`](investigations/receipts-flakes.md) |

All five are workers-only (or overwhelmingly so): the flake story is
**worker-deployment races**, split into two classes:
1. **Read-after-write via background/async processing** (room stats, receipts
   fan-out) — the old #15006/#14902 architectural class recurring.
2. **Event-auth/state races on out-of-band invites + federation** (#19858).

Lower-frequency but recurring: `TestPartialStateJoin/Device_list_tracking/*`
(3, the perennial faster-joins family), `TestJumpToDateEndpoint` (2),
`TestDelayedEvents` (2-3, spread over arrangements).

Deep-dives: see `investigations/*.md` (room-summary fix, receipts, federation
invites).

## Not flakes (would pollute naive counts)

- `TestMSC4311FullCreateEventOnStrippedState` ×12, `TestMSC4429ProfileUpdates`
  ×6: single branch each — legit failures on one PR.
- `tests.federation.test_federation_join_upgraded_room.*` (trial, 30 fails,
  12 on develop): develop was genuinely broken 07-28→07-29, fixed next day.

## The msc4429 incident: cross-repo coordination failure (46 jobs, ~23% of complement failures)

46 failed complement jobs (07-31→08-10, 8+ branches incl. develop ×11) had
**zero test failures**: `go test` exited 1 because the hardcoded package list
in `scripts-dev/complement.sh` included `./tests/msc4429`
(added by synapse PR #19556 on **08-06**) while the Complement checkout
(fallback = `HEAD` of matrix-org/complement) didn't contain `tests/msc4429`
until complement#849 merged on **08-10**:

```
stat .../complement/tests/msc4429: directory not found
FAIL ./tests/msc4429 [setup failed]
```

Made worse by two tooling bugs that hid the real error:
- The jq progress filter drops package-level JSON events (they have no
  `.Test`), so the terminal showed *all tests passing* then "exit code 1".
- gotestfmt v2.5.0 **panics** on `build-fail`/`build-output` events
  (`panic: BUG: Empty package name encountered` — they carry `ImportPath`,
  not `Package`), so the formatted-log step crashed instead of showing the
  build error. Upstream is effectively unmaintained (gotestfmt#64 open, last
  release 2023), so no fix is coming: either pre-filter build events with jq
  before gotestfmt, or move to gotestsum (which also solves retries, see
  recommendation 4).

Fixes: (a) make `complement.sh` filter its package list to directories that
exist (warn loudly on skip) or use `./tests/...` wildcards; (b) sequence such
changes complement-first; (c) surface package-level fail events in the jq
filter; (d) fix/upgrade gotestfmt handling of build failures.

Note the earlier occurrences (07-31, 08-03 on `madlittlemods/*` branches)
predate #19556 landing on develop — those branches carried the same change
before merge, consistent with the same mechanism.

Remaining 2 unparsed failures: docker image build errors (infra).

## Trial: no real flake problem right now

65 failed trial jobs in the window decompose into: the 07-28 develop breakage
(above), single-PR legit failures, and 4 catastrophic whole-run failures
(hundreds of `[ERROR]`s) caused by import errors — e.g. `trial-olddeps`
failing with `ImportError: cannot import name 'Collector' from
'prometheus_client.registry'` (dependency floor too old for code on that
branch). Zero retry-confirmed trial flakes; the known #12870
(`EventPersisterShardTestCase.test_basic`) didn't fire in this window.

Implication: the pytest migration is a workflow/tooling play (better fixtures,
`-x`, `--lf`, rerun plugins, xdist), not urgent flake-fighting. The trial
lane's real gap is that `_trial_temp` logs aren't uploaded on failure, so
rare flakes like #12870 stay undiagnosable (noted in that issue).

## Recommendations (flake management)

Short term, highest leverage (✅ = implemented in this workspace as separate
jj changes, ready to PR):
1. ✅ **Fix #19905** (join_rule from live state) — removes ~45% of workers-job
   failures on its own. `investigations/room-summary-join-rule.md`.
   Follow-ups: `room_type` and `world_readable` race the same way.
2. ✅ **Fix the #15517 receipts race** (~16% of workers failures) — real
   production bug (stale badge counts), deterministic regression tests on
   both DB engines. `investigations/receipts-flakes.md`. Follow-up found
   during the fix: `event_push_actions.py` main-timeline top-up increments a
   leaked loop variable (`counts` instead of `main_counts`) — separate PR.
3. ✅ **Harden complement.sh + CI log filters** against the build-fail class.
   `investigations/ci-hardening.md`.
4. **TestFederationRoomsInvite = 5 real Synapse bugs** (4 unreported):
   nudge PR #19960 (fixes the /sync 500), then file/fix the rest — most
   notably the repo-wide `@cachedList` exception-swallowing bug and
   `EventBuilder.build` mutating its caller's `prev_event_ids`.
   `investigations/federation-invite-flakes.md`.
5. **TestThreadReceiptsInSyncMSC4102**: unblock the MSC4102 interpretation
   dispute on #19838; land complement#881 regardless.

Structural:
6. **Retry-with-reporting instead of silent retry**: wrap the Complement run
   in `gotestsum --rerun-fails=2 --rerun-fails-max-failures=10
   --packages=...` (it drives `go test -json` itself, so the existing
   tee/gotestfmt/artifact flow keeps working via `--jsonfile`/`--raw-command`).
   Job goes green if the retry passes but the summary + JUnit output records
   every retried test → flakes stop blocking merges *without* becoming
   invisible. Complement tests are Docker-heavy but per-test rerun only
   re-runs the failed tests, and `-p 1` is preserved via `--packages` + `--`
   passthrough.
7. **Automated flake tracking from artifacts**: the workflow already uploads
   raw `go test -json` logs for every run. A small scheduled job (essentially
   `scripts/aggregate.py` productionized) can aggregate the last N runs,
   auto-update a pinned issue / dashboard with per-test failure rates and
   retry-confirmed flakes, replacing the hand-maintained #18537 checklist.
   Signal definitions that worked well here: retry-then-pass jobs, and
   develop-push failures; branch/day spread separates flakes from broken PRs.
8. **Quarantine mechanism with teeth**: a `-skip` regexp (or build-tag list)
   in-repo for known flakes, each entry requiring a tracking issue; the
   scheduled job from (5) flags quarantined tests that stopped flaking so
   they get re-enabled. Keeps the signal of the lane green while making debt
   visible in-repo instead of in CI noise.
9. For trial: keep as-is until pytest; but **upload `_trial_temp`** (or at
   least the failing test's log) as artifact on failure so the rare flake is
   diagnosable. With pytest later: `pytest-rerunfailures` for the same
   retry-with-reporting pattern, plus standard JUnit-XML→dashboard tooling.

## Follow-ups / open questions

- The 146 cancelled complement jobs (mostly `fail-fast` cascades and force
  pushes) hide additional signal; not analyzed.
- Should the workers arrangement gate merges at all while its failure rate is
  ~50%? A soft-fail + auto-filed-issue mode (like the sytest #18507
  discussion) may be more honest until the top flakes are fixed — but with
  fixes 1–3 landed, gating becomes tenable again.
