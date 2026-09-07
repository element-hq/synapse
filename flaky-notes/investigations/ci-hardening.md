# CI hardening: undebuggable Complement build failures

## Failure class

`scripts-dev/complement.sh` passes a hardcoded list of Complement test
package directories (e.g. `./tests/msc4429`) to `go test`. The Complement
tree it runs against is fetched separately by
`.ci/scripts/checkout_complement.sh` (falling back to Complement HEAD) and
can lag behind what a Synapse branch expects — e.g. when Synapse adds an
MSC's test package to the list before the matching Complement PR merges
upstream. When that happens, the missing directory makes `go test` fail the
*build* for that package rather than fail a test.

This bit us for real: 46 CI jobs failed between 2026-08-06 and 2026-08-10
because Synapse referenced `./tests/msc4429` before matrix-org/complement
had merged it.

Two tooling problems made this near-impossible to diagnose from the CI logs
alone:

1. In `.github/workflows/complement_tests.yml`, the `jq` progress filter
   (three identical occurrences: sanity check step, main run step, in-repo
   run step) only ever printed `go test -json` events that had `.Test` set.
   Package-level `fail` events (`.Package` set, no `.Test`) and
   `build-output`/`build-fail` events (which carry `.ImportPath` instead of
   `.Package`) were silently dropped. The log showed every test passing,
   then just "exit code 1" — no indication a package failed to build at all.
2. gotestfmt v2.5.0 (unmaintained, installed via `@latest`, no upstream fix
   — gotesttools/gotestfmt#64) panics
   (`panic: BUG: Empty package name encountered`) on
   `build-output`/`build-fail` events, so the "Formatted ... logs" step
   crashed instead of rendering the build error.

## Changes made (kept deliberately small)

- `scripts-dev/complement.sh`: added `filter_existing_test_packages()`,
  used before the external-Complement `go test` invocation only (the
  in-repo list is `./tests/...` in this repo and always exists). It drops
  package entries whose directory doesn't exist, with a stderr `WARNING:`
  plus a GitHub `::warning` annotation (visible on the run summary page,
  not buried in the log), and errors out if nothing remains. Trade-off,
  noted in the function comment: a package renamed/removed upstream is
  skipped with a warning rather than failing CI — the alternative blocked
  all of CI for four days when the repos were last out of sync.
- `.github/workflows/complement_tests.yml`:
  - The progress-view `jq` filter (3 identical occurrences) prints compact
    `PASS/FAIL/SKIP <test> <elapsed>s` lines as before and drops known-noisy
    events (`run`/`output`/package-level `pass` etc.); **any other JSON
    event — package-level failures, build events, future event types — is
    passed through as raw JSON**. No per-event formatting logic to maintain,
    and nothing can silently vanish again.
  - The three "Formatted ... logs" steps pre-filter the log with a
    one-expression `jq` stage that drops lines whose JSON carries
    `.ImportPath` (build events, the only ones gotestfmt panics on),
    passing everything else through unchanged; plus `set -o pipefail` so a
    broken jq/gotestfmt fails the step.
- Added `changelog.d/20095.misc`.

## Verification

- jq filters exercised against sample lines covering: `build-output`,
  `build-fail`, package-level `fail`, test `pass`/`fail`, package
  `pass`/`skip`/`start`, non-JSON lines, bare numbers/null/arrays, and
  `{`-prefixed non-JSON — correct routing in all cases; non-build lines
  pass through unchanged.
- `bash -n` passes; empty-array expansion is guarded (bash 3.2-safe); the
  three occurrences of each jq filter are byte-identical; the workflow YAML
  parses.
