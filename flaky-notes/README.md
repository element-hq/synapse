# Complement / trial flaky test investigation

Started 2026-08-13. Workspace: jj workspace `syn-flaky-tests` off synapse develop.

## Goal

1. Quantify which Complement tests flake in CI (`tests.yml` → `complement / complement (arrangement, database)` jobs), especially `(workers, Postgres)`.
2. Same for trial (unit test) jobs.
3. Root-cause the top offenders; propose fixes.
4. Propose better flake management (retries/quarantine/tracking), and a story for trial (long-term: pytest migration).

## Methodology

- `scripts/fetch_runs_and_jobs.sh`: last N=400 runs of `tests.yml` (all events/branches), per-job conclusions across **all run attempts** (GH API `filter=all`).
- `scripts/fetch_failed_logs.sh`: raw job logs for every failed complement/trial job.
- `scripts/aggregate.py`: parses the compact `FAIL <test> <elapsed>s` lines (from the jq view in the workflow) + trial `[FAIL]` lines; classifies infra failures (docker, disk, cancellation); aggregates per test.

Flake signal strength, strongest first:
1. Job failed in attempt N, same job succeeded in attempt N+1 (retry-confirmed flake).
2. Failure on a `develop` push run (code already passed PR CI + merge).
3. Repeated low-frequency failure of the same test across many unrelated PRs.

Data lives in `data/` (gitignored). `data/report.md` + `data/summary.json` are the outputs.

## Sources

- GH issue sweep: see `gh-issues.md`.
- Complement checkout: `../complement`.

## Concrete data points

### 2026-08-13 run 31690154578 (develop push, workers+Postgres)

`TestFederationRoomsInvite/Parallel/Remote_invited_user_can_reject_invite_when_homeserver_is_already_participating_in_the_room`

```
client.go:260: CSAPI.Must: POST .../rooms/!eAVgpXQhjFDGYzddsG:hs1/leave
  returned non-2xx code: 403 Forbidden
  body: {"errcode":"M_FORBIDDEN","error":"No create event in auth events"}
```

Test (complement/tests/federation_rooms_invite_test.go:132): hs2 already participates
(bob joined), bob2 rejects invite → local `/leave` on hs2 403s. Looks like a
workers-mode race where the leave is auth'd before the room's auth chain /
current state is fully available on the worker handling the request
("No create event in auth events" comes from event_auth on an auth_events set
missing m.room.create).
