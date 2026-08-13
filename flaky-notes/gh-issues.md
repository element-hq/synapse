# GitHub issues on flaky tests — Synapse / Complement ecosystem

Research date: 2026-08-13. Searched `element-hq/synapse` (and its predecessor
`matrix-org/synapse`, now archived/redirects to element-hq) and
`matrix-org/complement` via `gh search issues` and the `Z-Flake` label.

Note on dates: issues transferred from `matrix-org/synapse` to
`element-hq/synapse` in Dec 2023 all show `createdAt` around 2023-12-19/21 in
the new repo (migration timestamp). The **original** creation dates below are
taken from the old `matrix-org/synapse` repo where the issue number still
resolves there.

Both repos use a common label **`Z-Flake`** ("Tests that give intermittent
failures") for this category.

---

## 1. Synapse — Complement flakes (label `Z-Flake` + search)

All labelled `Z-Flake` on `element-hq/synapse`, cross-referenced with original
dates from `matrix-org/synapse`:

| # | Title | State | Created (orig.) | Gist |
|---|-------|-------|------------------|------|
| [16310](https://github.com/element-hq/synapse/issues/16310) | `Device_list_tracking_for_pre-existing_members_in_partial_state_room` flake | Open | 2023-09-13 | Complement device-list test under partial-state joins; timing-sensitive. |
| [16097](https://github.com/element-hq/synapse/issues/16097) | `TestClientSpacesSummary/query_whole_graph` flake | Open | 2023-08-10 | Space summary graph traversal test, intermittent. |
| [16020](https://github.com/element-hq/synapse/issues/16020) | `TestFederationKeyUploadQuery/.../Can_query_remote_device_keys_using_POST` flake | Open | 2023-07-28 | Remote device-key query over federation. |
| [15750](https://github.com/element-hq/synapse/issues/15750) | `TestRestrictedRoomsRemoteJoinLocalUserInMSC3787Room` flake | Open | 2023-06-09 | MSC3787 restricted-room remote join. |
| [15637](https://github.com/element-hq/synapse/issues/15637) | `TestRestrictedRoomsRemoteJoinInMSC3787Room/Join_should_fail_when_left_allowed_room` flake | Open | 2023-05-20 | Same family; failure-mode assertion is flaky. |
| [15517](https://github.com/element-hq/synapse/issues/15517) | `TestThreadedReceipts` flake? | Open | 2023-05-02 | Threaded read receipts. |
| [15408](https://github.com/element-hq/synapse/issues/15408) | `TestPartialStateJoin/MembersRequestBlocksDuringPartialStateJoin` flake | Open | 2023-04-06 | Partial-state join blocking behaviour on `/members`. |
| [15299](https://github.com/element-hq/synapse/issues/15299) | `Newly_joined_room_is_included_in_an_incremental_sync` flake(?) | Open | 2023-03-21 | Incremental sync race after join. |
| [15106](https://github.com/matrix-org/synapse/issues/15106) | `TestRoomCanonicalAlias/.../m.room.canonical_alias_accepts_present_aliases` | **Closed** | 2023-02-20 | Old repo only (closed, not carried to Z-Flake list on new repo query but label present there too). |
| [15086](https://github.com/element-hq/synapse/issues/15086) | `TestPartialStateJoin/Can_change_display_name_during_partial_state_join` flakey | Open | 2023-02-16 | Display-name propagation during resync. |
| [15039](https://github.com/matrix-org/synapse/issues/15039) | `TestPartialStateJoin/.../Device_list_updates_reach_incorrectly_kicked_servers...` | Closed | 2023-02-09 | Device-list fan-out edge case; fixed. |
| [15006](https://github.com/element-hq/synapse/issues/15006) | `TestPartialStateJoin/Room_stats_are_correctly_updated_once_state_re-sync_completes` flakey | Open | 2023-02-07 | Room stats update timing after resync — same read-after-write class as #19905 below. |
| [14986](https://github.com/matrix-org/synapse/issues/14986) | `TestRestrictedRoomsRemoteJoinFailOver` flakey | Closed | 2023-02-03 | |
| [14902](https://github.com/matrix-org/synapse/issues/14902) | `User_directory_is_correctly_updated_once_state_re-sync_completes` flakey | Closed | 2023-01-23 | Same read-after-write resync class. |
| [14895](https://github.com/matrix-org/synapse/issues/14895) | `PartialStateJoinSyncsUsingOtherHomeservers` flakey under workers | Closed | 2023-01-23 | Worker-mode specific. |
| [14853](https://github.com/matrix-org/synapse/issues/14853) | `TestPartialStateJoin/.../Device_list_updates_reach_incorrectly_absent_servers...` | Closed | 2023-01-16 | |
| [14585](https://github.com/element-hq/synapse/issues/14585) | `TestRoomForget/Parallel/Can_re-join_room_if_re-invited` flakey | Open | 2022-11-30 | |
| [14572](https://github.com/element-hq/synapse/issues/14572) | `TestOlderLeftRoomsNotInLeaveSection` flakey | Open | 2022-11-28 | |
| [14543](https://github.com/matrix-org/synapse/issues/14543) | `TestPartialStateJoin/Room_aliases_can_be_added_and_queried_during_a_resync` | Closed | 2022-11-24 | |
| [14506](https://github.com/matrix-org/synapse/issues/14506) | `TestPartialStateJoin/.../Device_list_tracking_for_pre-existing_members...` | Closed | 2022-11-21 | Precursor of #16310 above; same area recurred years later. |
| [14475](https://github.com/matrix-org/synapse/issues/14475) | `TestPartialStateJoin/.../Device_list_updates_reach_all_servers_in_partial_state_rooms` | Closed | 2022-11-17 | Same test recurs (see #19171 area) after fixes regress. |
| [14432](https://github.com/matrix-org/synapse/issues/14432) | `TestPartialStateJoin/.../Device_list_updates_reach_incorrectly_kicked_servers...` | Closed | 2022-11-14 | |
| [14306](https://github.com/matrix-org/synapse/issues/14306) | `TestPartialStateJoin/Device_list_tracking/.../new_member_leaves_partial_state_room` | Closed | 2022-10-26 | |
| [14256](https://github.com/element-hq/synapse/issues/14256) | `TestClientSpacesSummaryJoinRules` flake | Open | 2022-10-21 | |
| [14245](https://github.com/matrix-org/synapse/issues/14245) | `TestPartialStateJoin/Lazy-loading_initial_sync_includes_remote_memberships...` | Closed | 2022-10-20 | |
| [14226](https://github.com/matrix-org/synapse/issues/14226) | `TestKnocking/Users_in_the_room_see_a_user's_membership_update_when_they_knock` | Closed | 2022-10-18 | |
| [14183](https://github.com/matrix-org/synapse/issues/14183) | `GET_/rooms/:room_id/aliases_lists_aliases` flake(?) | Closed | 2022-10-14 | |
| [14103](https://github.com/matrix-org/synapse/issues/14103) | `TestDeviceListUpdates/when_remote_user_joins_a_room` flake(?) | Closed | 2022-10-07 | |
| [14048](https://github.com/matrix-org/synapse/issues/14048) | `CanReceiveEventsWithHalfMissingGrandparentsDuringPartialStateJoin` flakey | Closed | 2022-10-04 | |
| [14010](https://github.com/matrix-org/synapse/issues/14010) | `TestPartialStateJoin/.../Device_list_updates_reach_newly_joined_servers...` | Closed | 2022-10-03 | Same test flakes again later, see #18537 tracker. |
| [13977](https://github.com/matrix-org/synapse/issues/13977) | `TestPartialStateJoin/.../Device_list_updates_no_longer_reach_departed_servers...` | Closed | 2022-09-30 | |
| [13945](https://github.com/matrix-org/synapse/issues/13945) | `TestPartialStateJoin/Rejects_send_knock_during_partial_join` flakey(?) | Closed | 2022-09-29 | |
| [13944](https://github.com/matrix-org/synapse/issues/13944) | `can_paginate_after_getting_remote_event_from_timestamp_to_event_endpoint` flakey(?) | Closed | 2022-09-29 | |
| [13828](https://github.com/element-hq/synapse/issues/13828) | `TestInviteFromIgnoredUsersDoesNotAppearInSync` seems flakey | Open | 2022-09-16 | Also seen from the Complement side, see #349 below. |
| [13777](https://github.com/element-hq/synapse/issues/13777) | `TestPartialStateJoin/Resync_completes_even_when_events_arrive_before_their_prev_events` | Open | 2022-09-12 | |
| [13565](https://github.com/matrix-org/synapse/issues/13565) | `TestPartialStateJoin/State_rejected_incorrectly` | Closed | 2022-08-19 | |
| [13564](https://github.com/matrix-org/synapse/issues/13564) | `TestPartialStateJoin/CanReceiveEventsWithMissingParentsDuringPartialStateJoin` | Closed | 2022-08-19 | |
| [13508](https://github.com/element-hq/synapse/issues/13508) | `TestImportHistoricalMessages/.../new_historical_messages_are_visible_in_next_scroll_back...` | Open | 2022-08-11 | |
| [13334](https://github.com/matrix-org/synapse/issues/13334) | `TestWriteMDirectAccountData` flakey | Closed | 2022-07-20 | |
| [13199](https://github.com/matrix-org/synapse/issues/13199) | `Existing members see new members' presence` flaky | Closed | 2022-07-06 | |
| [12798](https://github.com/matrix-org/synapse/issues/12798) | `TestMediaFilenames` fails in Complement under workers | Closed | 2022-05-19 | |

**Overwhelming pattern**: the majority of persistent Complement flakes on
Synapse are in the **partial-state / faster-remote-join** area
(`TestPartialStateJoin/*`, device-list fan-out to other servers) — this
single feature generated a couple dozen separate flake issues from 2022–2023,
almost all closed as fixed but recurring under slightly different names for
years. The second recurring cluster is **read-after-write consistency in
worker deployments** (room stats / user directory / spaces summary being
updated asynchronously in the background while a Complement test expects the
update to be visible immediately after the state change) — this pattern
resurfaces again in 2026 (see #19905 below), so it's a long-standing
architectural sore spot, not a one-off bug.

### More recent Synapse Complement flakes (2025–2026, from free-text search)

- **[#18537 — "\[Meta\] List of flaky Synapse tests"](https://github.com/element-hq/synapse/issues/18537)** (open, created 2025-06-10). **The central flake-tracking issue** — see tooling section below for detail. Currently tracks, among others:
  - `twisted.protocols.amp.TooLong` in `trial (3.9, sqlite, all)` — mitigated by PR [#18736](https://github.com/element-hq/synapse/pull/18736) and [#19832](https://github.com/element-hq/synapse/pull/19832) (checked off).
  - `tests.replication.test_sharded_event_persister.EventPersisterShardTestCase.test_basic` — still open, same test as standalone issue #12870 below.
  - `tests.rest.admin.test_user.UserRedactionBackgroundTaskTestCase.test_redact_messages_all_rooms` in postgres trial — fixed by PR [#19890](https://github.com/element-hq/synapse/pull/19890) (checked off).
  - Sytest `After /purge_history users still get pushed for new messages` — recurring across many runs, unresolved.
  - Sytest `Newly joined room includes presence in incremental sync` — recurring.
  - Sytest `(bullseye, multi-postgres, workers[, asyncio])` — "many tests" fail in batches; this configuration is treated as the worst offender (see #18507).
  - Complement `TestFederationRoomsInvite` (several subtests: reject invite while already participating; reject invite repeatedly over federation; rescind invite over federation) — recurring across many PRs/runs since mid-2025; spun off standalone issue #19858.
  - Complement `TestPartialStateJoin/Outgoing_device_list_updates/*` — same historical partial-state cluster recurring again.

- **[#19858 — `TestFederationRoomsInvite` flake hitting unreachable `Exception: Missing state for event that is not user's own membership`](https://github.com/element-hq/synapse/issues/19858)** (open, created 2026-06-15). Sync handler throws on missing state for a membership event; root cause looked like a genuine Synapse bug surfaced by the flaky reproduction, not purely a test-timing issue. Cross-linked to #18537.

- **[#19905 — `TestRoomSummaryAllowedRoomIDs/restricted_room_includes_allowed_room_ids` is flaky](https://github.com/element-hq/synapse/issues/19905)** (open, created 2026-07-02). Root-caused: `join_rule` is read from the `room_stats_state` table, which is populated asynchronously by a background task on the worker that runs background tasks; under Complement's worker deployment the state isn't updated by the time the test calls `/room_summary`, unlike `allowed_room_ids` which is read live from the state event. A community commenter proposed reading `join_rule` from the live state event too, matching `allowed_room_ids`'s approach, as the fix. Same architectural class as the older #15006/#14902 read-after-write flakes.

- **[#19171 — Complement `TestThreadReceiptsInSyncMSC4102` is flaky](https://github.com/element-hq/synapse/issues/19171)** (open, created 2025-11-12). Flaky specifically when Synapse runs with workers; remote federated server sometimes doesn't see the unthreaded receipt as expected. Author suspects genuine Synapse bug, not a bad test. Also affects `synapse-rust-apps` CI. Closed-duplicate follow-up **[#19908](https://github.com/element-hq/synapse/issues/19908)** (closed 2026-07-03) reproduces essentially the same failure with full logs, "triggered occasionally during Complement (workers, Postgres) runs," also observed on Rust homeserver implementations, so possibly a race outside Synapse's control (Complement itself, or Twisted).

- **[#19907 — `TestMessagesOverFederation/Backfill_from_nearby_backward_extremities_past_token` flakes](https://github.com/element-hq/synapse/issues/19907)** (closed 2026-07-03). Reported as ~1-in-20/50 runs; join over federation returns `502 Bad Gateway`/`Failed to make_join via any server`. Also affects `TestJoinViaRoomIDAndServerName` in the reporter's fork. Overlaps with Complement issue #887 (below), which does deep root-cause analysis of the same underlying test.

- **[#18405 — CI test suites are flaky](https://github.com/element-hq/synapse/issues/18405)** (closed 2025-05-09). General "test failures increasing on develop" tracking issue. Root cause turned out to be Docker 26→28 upgrade on GitHub's `ubuntu24.04` runner image changing container networking behaviour, producing `address already in use` / container name conflicts — fixed upstream in Complement by PR [#776](https://github.com/matrix-org/complement/pull/776) (closing Complement issue #775). Notable community comment (3nprob) pushed back on the "accept flaky tests" culture, arguing failing `develop` jobs should be retried/confirmed passing before more commits land, and that external contributors are disadvantaged because they can't trigger CI re-runs themselves.

- **[#18507 — The `sytest (bullseye, multi-postgres, workers, asyncio)` CI job is flaky](https://github.com/element-hq/synapse/issues/18507)** (closed 2025-06-09). See tooling section — proposed disabling vs. soft-failing the whole CI configuration.

---

## 2. matrix-org/complement — issues about flaky tests / CI reliability

| # | Title | State | Created | Gist |
|---|-------|-------|---------|------|
| [887](https://github.com/matrix-org/complement/issues/887) | `TestMessagesOverFederation` detects critical regressions but NOT reliably | Closed | 2026-07-04 | Extensive (AI-assisted) root-cause writeup: a timing/race issue in backfill-after-rejoin, ~75% pass rate on standard runners even with a real regression present, worse on ARM, possibly v11 vs v12 room-version dependent. Concern raised that a test meant to catch "nearly fatal" backfill/state-res regressions is itself unreliable enough to give false confidence. |
| [868](https://github.com/matrix-org/complement/issues/868) | `TestJumpToDateEndpoint/parallel` boundary subtests flake at millisecond granularity | Closed 2026-07-03 | 2026-04-30 | Two subtests sample `time.Now()` and compare against server timestamps that can land in the same millisecond, since `/timestamp_to_event` is spec'd as ms-granular. Maintainer (kegsay) accepted a `time.Sleep(2ms)` guard-sleep fix "with commentary explaining why." Good example of a maintainer explicitly endorsing sleep-based flake mitigation when the root cause is an inherent clock-granularity race, not a real bug. |
| [897](https://github.com/matrix-org/complement/issues/897) | Password change tests depend on oneshot UIA | Open | 2026-07-15 | UIA (User-Interactive Auth) helper does one-shot auth, which is not spec-compliant (spec requires receiving a session ID from a challenge before continuing the flow); causes flaky failures in all tests using the helper. |
| [898](https://github.com/matrix-org/complement/issues/898) | Account deactivation tests depend on oneshot UIA | Open | 2026-07-15 | Same underlying non-spec-compliant UIA helper issue as #897. |
| [899](https://github.com/matrix-org/complement/issues/899) | Device deletion tests depend on oneshot UIA | Open | 2026-07-15 | Same UIA helper issue, affects 4 device-management test call sites. |
| [893](https://github.com/matrix-org/complement/issues/893) | (root issue behind 897/898/899) UIA registration/auth not spec compliant, breaks non-Synapse homeservers | Open | (2026, referenced by 897-899) | Discovered while testing a third-party homeserver (Venator); documents that Complement's UIA client helper "one-shots" the auth challenge instead of properly following the UIA flow, which happens to work against Synapse's implementation quirks but is not spec-compliant and causes flakiness/incompatibility elsewhere. |
| [751](https://github.com/matrix-org/complement/issues/751) | `TestOutboundFederationIgnoresMissingEventWithBadJSONForRoomVersion6` causes other tests to be flaky | Open | 2024-12-19 | One test intentionally sends malformed JSON (a float where an int is expected) to test rejection behaviour; this appears to leak/pollute state such that unrelated tests (`TestFederationKeyUploadQuery`, `TestKnockingInMSC3787Room`, `TestToDeviceMessagesOverFederation`, etc.) fail afterward. Root cause (test isolation failure) never resolved as of research date. |
| [775](https://github.com/matrix-org/complement/issues/775) | `address already in use` container networking errors (May 2025) | Closed 2025-05-08 | 2025-05-06 | Traced to GitHub bumping the `ubuntu24.04` runner's Docker from 26.1.3→28.0.4, which changed container networking behavior; referenced moby/moby#49935. Fixed by #776. |
| [776](https://github.com/matrix-org/complement/issues/776) *(PR, tracked as fix)* | Networking fix for Docker v28 | Closed 2025-05-08 | — | Complement was calling `PublishAllPorts` in addition to explicit `PortBindings` (needed for a niche Homerunner use case), which Docker ≥28 no longer tolerates; also fixed a missing container cleanup on failed `ContainerStart` that caused follow-on "container name already in use" conflicts. |
| [720](https://github.com/matrix-org/complement/issues/720) | Homerunner network creation seems to race deployment | Open | 2024-04-11 | `network ... not found` error; maintainer (kegsay) confirms Complement waits for the network-creation call to return but if Docker's create is async under the hood, containers can still race ahead of the network actually being ready. |
| [400](https://github.com/matrix-org/complement/issues/400) | Homerunner `/create` intermittently fails: "No images have been built for blueprint" | Open | 2022-06-30 | Mitigated (not fully fixed) by raising `HOMERUNNER_SPAWN_HS_TIMEOUT_SECS` from the 30s default to 50s. |
| [595](https://github.com/matrix-org/complement/issues/595) | `TestFederatedEventRelationships` seems flakey | Open | 2023-02-01 | Split out from #568 below; recurs in CI. |
| [594](https://github.com/matrix-org/complement/issues/594) | `TestEventRelationships` seems flakey | Open | 2023-02-01 | Split out from #568. |
| [568](https://github.com/matrix-org/complement/issues/568) | Event relationship tests seem flakey | Closed 2023-01-10 | 2022-12-10 | Parent issue for #594/#595, closed but the two child issues remain open — flakiness apparently outlived the "fix." |
| [518](https://github.com/matrix-org/complement/issues/518) | `TestSendAndFetchMessage` marked "Flakey" on Dendrite | Open | 2022-10-16 | Tracks whether an existing skip/flaky-marker comment in the test source can be cleared once the underlying Dendrite bug (or test fragility) is fixed. |
| [461](https://github.com/matrix-org/complement/issues/461) | Suspected flake: `sync_should_succeed_even_if_the_sync_token_points_to_a_redaction_of_an_unknown_event` | Open | 2022-09-14 | Reporter notes if it's a genuine Synapse bug rather than a test problem, it should move to the Synapse repo — illustrates the recurring ambiguity of "is this a flaky test or a real intermittent bug." |
| [349](https://github.com/matrix-org/complement/issues/349) | `TestUnrejectRejectedEvents` and `TestInviteFromIgnoredUsersDoesNotAppearInSync` seem flaky | Closed | 2022-03-23 | Random CI failures (`net/http: request canceled` on a `/sync` long-poll); rerunning fixed it — no root cause found, just noted as flaky. Cross-references the Synapse-side #13828 for the invite/ignored-user test. |
| [291](https://github.com/matrix-org/complement/issues/291) | Unexplained failure (flake?) in `TestFetchHistoricalJoinedEventDenied` | Closed | 2022-01-25 | `createRoom` returned 401 once, passed on rerun; root cause never determined ("unclear if complement is at fault or the test"). |
| [242](https://github.com/matrix-org/complement/issues/242) | Allow "user-defined" list of skipped tests | Closed | 2021-12-01 | Not strictly a flake report — a tooling proposal (see section 4) for a `complement.yml` skip/allow-list so third-party homeservers not closely coupled to Matrix.org/Element's dev cycle can manage incompatible/known-bad tests without needing upstream PRs merged first. |
| [580](https://github.com/matrix-org/complement/issues/580) | `TestDeviceListUpdates/when_joining_a_room_with_a_remote_user` is flakey | Closed | 2022-09-29 | The only issue on Complement carrying the `Z-Flake` label explicitly (label otherwise barely used on this repo). |

---

## 3. Trial / unit-test flakes (Synapse)

Far fewer of these turn up than Complement flakes — Synapse's own unit-test
suite (`trial`) is comparatively stable; most flake reports are federation/
worker integration tests.

- **[#12870 — `EventPersisterShardTestCase.test_basic` is flaky](https://github.com/element-hq/synapse/issues/12870)** (open, orig. created 2022 on matrix-org/synapse, migrated). `self.assertTrue(persisted_on_1)` fails intermittently; no logs retained (`_trial_temp` isn't preserved in CI artifacts) so root cause was never diagnosed. Still open and still listed as unresolved in the #18537 meta-tracker.
- Via #18537 (meta issue): `twisted.protocols.amp.TooLong` flaking in `trial (3.9, sqlite, all)` — resolved by PRs #18736 and #19832.
- Via #18537: `UserRedactionBackgroundTaskTestCase.test_redact_messages_all_rooms` flaking in postgres trial runs — resolved by PR #19890.
- Sytest jobs (`bullseye`, `bullseye+postgres`, `bullseye+multi-postgres+workers[+asyncio]`) are the most consistently flaky CI lane overall — see #18507 and #18405 discussion. Sytest is Perl-based and legacy; comments in #18507 show a Synapse maintainer (MadLittleMods) saying he'd rather port remaining Sytest coverage to Complement than invest in fixing Sytest's flakiness.

---

## 4. Flake-management / tooling discussions

- **[element-hq/synapse#18537 — "\[Meta\] List of flaky Synapse tests"](https://github.com/element-hq/synapse/issues/18537)** (open). This is Synapse's live, manually-curated flake registry: a checklist of test names grouped by CI job/lane, each with links to multiple failing Action runs, and a checkbox + linked PR once fixed. It is explicitly a stopgap ("compile a central list... so they can be disabled/fixed... This list should be edited by the team as flakes are found") rather than automated tooling — no retry-bot, no quarantine mechanism, no gotestfmt-based flake detection; just a hand-maintained GitHub issue. New per-test issues (e.g. #19171, #19858, #19905) get filed and cross-linked back into this meta-issue as they're found, and some entries reference dedicated fix PRs (#18736, #19832, #19890).

- **[element-hq/synapse#18507 — sytest job flaky](https://github.com/element-hq/synapse/issues/18507)** (closed). Real policy debate about how to handle a chronically flaky CI *lane* (not a single test): options floated were (a) disable the configuration outright, or (b) keep running it but make CI ignore its result (soft-fail) — maintainer opened PR #18506 as an example of option (b). Resolution per the linked weekly backend-meeting notes: restrict "retry/nudge CI" access to team members who need it, rather than building automated retry tooling. Maintainer comment states the team would rather migrate remaining Sytest-only coverage to Complement than fix Sytest's flakiness directly, and flags the desire for "a full list of flakes ... at some point" (which became #18537, filed the next day).

- **[element-hq/synapse#18405 — CI test suites are flaky](https://github.com/element-hq/synapse/issues/18405)** (closed). Contains the most explicit pushback found in this research against Synapse's informal "accept some flakiness" stance: a contributor (3nprob) argues that failing `develop`-branch jobs should be auto-retried and confirmed green before further commits land, that tolerating "flaky tests" is hard to distinguish from tolerating unstable software, and that external contributors are structurally disadvantaged since they lack permission to trigger CI re-runs on their own PRs. No process change appears to have resulted directly from this comment (see #18507 resolution above — access was still restricted to team members rather than opened up or automated).

- **[matrix-org/complement#242 — Allow a "user-defined" list of skipped tests](https://github.com/matrix-org/complement/issues/242)** (closed). Early (2021) proposal for a `complement.yml` skip/allow-list mechanism so that third-party/independent homeserver implementations not tightly coupled to Matrix.org's/Element's development cadence could manage known-incompatible or known-flaky tests locally, without needing a `runtime.SkipIf` PR merged upstream first. Framed as a decentralization/inclusivity concern for homeservers outside the Synapse/Dendrite core group.

- **[matrix-org/complement#893](https://github.com/matrix-org/complement/issues/893)** and its three children **#897/#898/#899**: a cluster of 2026-07 issues (from a third-party homeserver author testing against Complement) that reframe several "flaky test" symptoms as a single root cause — Complement's UIA (User-Interactive Auth) test helper does a non-spec-compliant "one-shot" of the auth flow that happens to work against Synapse's specific behavior but causes flaky failures on stricter/other implementations, and is fragile even against Synapse. This is the closest thing found to a "flakiness caused by test-harness spec non-compliance" write-up, as opposed to timing races or infra issues.

- No evidence found of: a gotestfmt-based flake dashboard, an automated test-retry GitHub Action, or a formal "quarantine" label/mechanism in either repo. Retry handling in practice is manual (maintainers with repo permissions re-running failed Actions jobs), which #18507's resolution and #18405's contributor pushback both call out directly as a friction point, especially for external contributors.

---

## Most relevant open issues (shortlist)

1. **[element-hq/synapse#18537 — \[Meta\] List of flaky Synapse tests](https://github.com/element-hq/synapse/issues/18537)** — the living flake registry; start here for current state of known flakes across trial/sytest/Complement.
2. **[element-hq/synapse#19858 — `TestFederationRoomsInvite` flake / "Missing state for event that is not user's own membership"](https://github.com/element-hq/synapse/issues/19858)** — actively recurring, possibly masking a real sync-handler bug, cross-linked to #18537.
3. **[element-hq/synapse#19905 — `TestRoomSummaryAllowedRoomIDs` flaky](https://github.com/element-hq/synapse/issues/19905)** — well-diagnosed read-after-write worker-replication race, with a proposed one-line fix in comments; good candidate to actually pick up.
4. **[element-hq/synapse#19171 — `TestThreadReceiptsInSyncMSC4102` is flaky](https://github.com/element-hq/synapse/issues/19171)** — worker-mode receipts-over-federation bug, affects downstream Rust CI too; detailed logs already gathered.
5. **[element-hq/synapse#12870 — `EventPersisterShardTestCase.test_basic` is flaky](https://github.com/element-hq/synapse/issues/12870)** — oldest unresolved unit-test flake, still with no root cause (logs were never captured).
6. **[matrix-org/complement#887 — `TestMessagesOverFederation` unreliable at detecting regressions](https://github.com/matrix-org/complement/issues/887)** — highest-severity write-up found; argues a test meant to catch severe backfill/state-res regressions only catches them ~75% of the time.
7. **[matrix-org/complement#751 — one test polluting others (`TestOutboundFederationIgnoresMissingEventWithBadJSONForRoomVersion6`)](https://github.com/matrix-org/complement/issues/751)** — genuine test-isolation bug, still open/unaddressed since Dec 2024.
8. **[matrix-org/complement#893 (+ #897/#898/#899) — UIA helper non-spec-compliance](https://github.com/matrix-org/complement/issues/893)** — a fresh (2026-07) cluster reframing several flaky-auth-test symptoms as one fixable root cause in the test harness itself.
9. **[element-hq/synapse#18405 — CI test suites are flaky](https://github.com/element-hq/synapse/issues/18405)** *(closed but instructive)* — best discussion of process/culture around accepting flakiness, and the external-contributor-CI-retry friction point.
