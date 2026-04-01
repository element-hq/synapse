#!/usr/bin/env bash
# This script is designed for developers who want to test their code
# against Complement.
#
# It makes a Synapse image which represents the current checkout,
# builds a synapse-complement image on top, then runs tests with it.
#
# By default the script will fetch the latest Complement main branch and
# run tests with that. This can be overridden to use a custom Complement
# checkout by setting the COMPLEMENT_DIR environment variable to the
# filepath of a local Complement checkout or by setting the COMPLEMENT_REF
# environment variable to pull a different branch or commit.
#
# By default Synapse is run in monolith mode. This can be overridden by
# setting the WORKERS environment variable.
#
# You can optionally give a "-f" argument (for "fast") before any to skip
# rebuilding the docker images, if you just want to rerun the tests.
#
# Remaining commandline arguments are passed through to `go test`. For example,
# you can supply a regular expression of test method names via the "-run"
# argument:
#
# ./complement.sh -run "TestOutboundFederation(Profile|Send)"

# Exit if a line returns a non-zero exit code
set -e

# Tag local builds with a dummy registry namespace so that later builds may reference
# them exactly instead of accidentally pulling from a remote registry.
#
# This is important as some Docker storage drivers/types prefer remote images over local
# (like `containerd`) which causes problems as we're testing against some remote image
# that doesn't include all of the changes that we're trying to test (be it locally or in
# a PR in CI). This is spawning from a real-world problem where the GitHub runners were
# updated to use Docker Engine 29.0.0+ which uses `containerd` by default for new
# installations.
#
# XXX: If the Docker image name changes, don't forget to update
# `.github/workflows/push_complement_image.yml` as well
LOCAL_IMAGE_NAMESPACE=localhost

# XXX: If the Docker image name changes, don't forget to update
# `.github/workflows/push_complement_image.yml` as well
COMPLEMENT_SYNAPSE_IMAGE_PATH="$LOCAL_IMAGE_NAMESPACE/complement-synapse"

# Helper to emit annotations that collapse portions of the log in GitHub Actions
echo_if_github() {
  if [[ -n "$GITHUB_WORKFLOW" ]]; then
    echo $*
  fi
}

# Helper to print out the usage instructions
usage() {
    cat >&2 <<EOF
Usage: $0 [-f] <go test arguments>...
Run the complement test suite on Synapse.
  --in-repo
        Whether to run the in-repo suite of Complement tests (see \`./complement\` in this project)
        vs the Complement tests from the Complement repo.

  -f, --fast
        Skip rebuilding the docker images, and just use the most recent
        'localhost/complement-synapse:latest' image.
        Conflicts with --build-only.

  --build-only
        Only build the Docker images. Don't actually run Complement.
        Conflicts with -f/--fast.

For help on arguments to 'go test', run 'go help testflag'.
EOF
}

# We use a function to wrap the script logic so that we can use `return` to exit early
# if needed. This is particularly useful so that this script can be sourced by other
# scripts without exiting the calling subshell (composable). This allows us to share
# variables like `SYNAPSE_SUPPORTED_COMPLEMENT_TEST_PACKAGES` with other scripts.
#
# Returns an exit code of 0 on success, or 1 on failure.
main() {
  # parse our arguments
  skip_docker_build=""
  skip_complement_run=""
  use_in_repo_tests=""
  while [ $# -ge 1 ]; do
    arg=$1
    case "$arg" in
      "-h")
        usage
        return 1
        ;;
      "--in-repo")
        use_in_repo_tests=1
        ;;
      "-f"|"--fast")
        skip_docker_build=1
        ;;
      "--build-only")
        skip_complement_run=1
        ;;
      *)
        # unknown arg: presumably an argument to gotest. break the loop.
        break
    esac
    shift
  done

  # Change to the repository root
  cd "$(dirname $0)/.."

  # Check for a user-specified Complement checkout
  if [[ -z "$COMPLEMENT_DIR" ]]; then
    COMPLEMENT_REF=${COMPLEMENT_REF:-main}
    echo "COMPLEMENT_DIR not set. Fetching Complement checkout from ${COMPLEMENT_REF}..."

    # Download the Complement checkout at the specified ref.
    wget -q https://github.com/matrix-org/complement/archive/${COMPLEMENT_REF}.tar.gz

    # Delete the existing complement checkout. Otherwise we'll end up with stale
    # test files after they're deleted server-side, and `tar` will not delete
    # old files.
    rm -rf complement-${COMPLEMENT_REF}

    # Extract the checkout.
    tar -xzf ${COMPLEMENT_REF}.tar.gz

    COMPLEMENT_DIR=complement-${COMPLEMENT_REF}
    echo "Checkout available at 'complement-${COMPLEMENT_REF}'"
  fi

  if [ -z "$skip_docker_build" ]; then
    # Figure out the Synapse version string from pyproject.toml
    synapse_version_string="$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")"

    # Build all complement images via docker bake
    echo_if_github "::group::Build Docker images via docker bake"
    SYNAPSE_VERSION_STRING="$synapse_version_string" \
      docker buildx bake complement
    echo_if_github "::endgroup::"

    echo "Docker images built."
  else
    echo "Skipping Docker image build as requested."
  fi

  # Default set of Complement tests to run from the Complement repo
  #
  # We pick and choose the specific MSC's that Synapse supports.
  default_complement_test_packages=(
    ./tests/csapi
    ./tests
    ./tests/msc3874
    ./tests/msc3890
    ./tests/msc3391
    ./tests/msc3757
    ./tests/msc3930
    ./tests/msc3902
    ./tests/msc3967
    ./tests/msc4140
    ./tests/msc4155
    ./tests/msc4306
    ./tests/msc4222
  )

  # Export the list of test packages as a space-separated environment variable, so other
  # scripts can use it.
  export SYNAPSE_SUPPORTED_COMPLEMENT_TEST_PACKAGES="${default_complement_test_packages[@]}"

  # Default set of Complement tests to run when using the in-repo test suite. Most
  # likely, this should be all tests.
  #
  # Relative to the `./complement` repo in this project
  default_in_repo_complement_test_packages=(
    ./tests/...
  )

  export COMPLEMENT_BASE_IMAGE="$COMPLEMENT_SYNAPSE_IMAGE_PATH"

  # Enable dirty runs, so tests will reuse the same container where possible.
  # This significantly speeds up tests, but increases the possibility of test pollution.
  export COMPLEMENT_ENABLE_DIRTY_RUNS=1

  # All environment variables starting with PASS_ will be shared.
  # (The prefix is stripped off before reaching the container.)
  export COMPLEMENT_SHARE_ENV_PREFIX=PASS_

  # * -count=1: Only run tests once, and disable caching for tests.
  # * -v: Output test logs, even if those tests pass.
  # * -tags=synapse_blacklist: Enable the `synapse_blacklist` build tag, which is
  #   necessary for `runtime.Synapse` checks/skips to work in the tests
  test_args=(
    -v
    -tags="synapse_blacklist"
    -count=1
  )

  # It takes longer than 10m to run the whole suite.
  test_args+=("-timeout=60m")

  if [[ -n "$WORKERS" ]]; then
    # Use workers.
    export PASS_SYNAPSE_COMPLEMENT_USE_WORKERS=true

    # Pass through the workers defined. If none, it will be an empty string
    export PASS_SYNAPSE_WORKER_TYPES="$WORKER_TYPES"

    # Workers can only use Postgres as a database.
    export PASS_SYNAPSE_COMPLEMENT_DATABASE=postgres

    # And provide some more configuration to complement.

    # It can take quite a while to spin up a worker-mode Synapse for the first
    # time (the main problem is that we start 14 python processes for each test,
    # and complement likes to do two of them in parallel).
    export COMPLEMENT_SPAWN_HS_TIMEOUT_SECS=120
  else
    export PASS_SYNAPSE_COMPLEMENT_USE_WORKERS=
    if [[ -n "$POSTGRES" ]]; then
      export PASS_SYNAPSE_COMPLEMENT_DATABASE=postgres
    else
      export PASS_SYNAPSE_COMPLEMENT_DATABASE=sqlite
    fi
  fi

  if [[ -n "$ASYNCIO_REACTOR" ]]; then
    # Enable the Twisted asyncio reactor
    export PASS_SYNAPSE_COMPLEMENT_USE_ASYNCIO_REACTOR=true
  fi

  if [[ -n "$UNIX_SOCKETS" ]]; then
    # Enable full on Unix socket mode for Synapse, Redis and Postgresql
    export PASS_SYNAPSE_USE_UNIX_SOCKET=1
  fi

  if [[ -n "$SYNAPSE_TEST_LOG_LEVEL" ]]; then
    # Set the log level to what is desired
    export PASS_SYNAPSE_LOG_LEVEL="$SYNAPSE_TEST_LOG_LEVEL"

    # Allow logging sensitive things (currently SQL queries & parameters).
    # (This won't have any effect if we're not logging at DEBUG level overall.)
    # Since this is just a test suite, this is fine and won't reveal anyone's
    # personal information
    export PASS_SYNAPSE_LOG_SENSITIVE=1
  fi

  # Log a few more useful things for a developer attempting to debug something
  # particularly tricky.
  export PASS_SYNAPSE_LOG_TESTING=1

  if [ -n "$skip_complement_run" ]; then
    echo "Skipping Complement run as requested."
    return 0
  fi

  # Print out the executed commands so it's more obvious what's happening at the end here.
  # Things are slightly ambiguous with the in-repo vs Complement tests.
  set -x

  if [ -n "$use_in_repo_tests" ]; then
    # Run the suite of Complement tests in the `./complement` directory in this repo
    cd "./complement"
    go test "${test_args[@]}" "$@" "${default_in_repo_complement_test_packages[@]}"
  else
    # Run the tests (from the Complement repo)!
    cd "$COMPLEMENT_DIR"
    go test "${test_args[@]}" "$@" "${default_complement_test_packages[@]}"
  fi

  # We don't need to print out executed commands anymore
  #
  # This is just `set +x` without printing `+ set +x` to the console (via
  # https://stackoverflow.com/questions/13195655/bash-set-x-without-it-being-printed/19226038#19226038)
  { set +x; } 2>/dev/null
}

main "$@"
# For any non-zero exit code (indicating some sort of error happened), we want to exit
# with that code.
exit_code=$?
if [ $exit_code -ne 0 ]; then
    exit $exit_code
fi
