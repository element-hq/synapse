# Contributor Guide

Synapse is a Python application that has Rust modules via pyo3 for performance.

## Dev Environment Tips
- Source code is primarily in `synapse/`, tests are in `tests/`.
- Run `poetry install --dev` to install development python dependencies. This will also build and install the Synapse rust code.
- Use `./scripts-dev/lint.sh` to lint the codebase (this attempts to fix issues as well). This should be run and produce no errors before every commit.

## Dev Tips
- If any change creates a breaking change or requires downstream users (sysadmins) to update their environment, call it out in `docs/upgrade.md` as a new entry with the title "# Upgrading to vx.yy.z" with the details of what they should do or be aware of.

## Testing Instructions
- Find the CI plan in the .github/workflows folder.
- Use `poetry run trial tests` to run all unit tests, or `poetry run trial tests.metrics.test_phone_home_stats.PhoneHomeStatsTestCase` (for example) to run a single test case. The commit should pass all tests before you merge.
- Some typing warnings are expected currently. Fix any test or type *errors* until the whole suite is green.
- Add or update relevant tests for the code you change, even if nobody asked.
