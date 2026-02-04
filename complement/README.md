# Complement testing

Complement is a black box integration testing framework for Matrix homeservers. It
allows us to write end-to-end tests that interact with real Synapse homeservers to
ensure everything works at a holistic level.


## Setup

Nothing beyond a [normal Complement
setup](https://github.com/matrix-org/complement?tab=readme-ov-file#running) (just Go and
Docker).


## Running tests

Run tests from the [Complement](https://github.com/matrix-org/complement) repo:

```shell
# Run the tests
./scripts-dev/complement.sh

# To run a whole group of tests, you can specify part of the test path:
scripts-dev/complement.sh ./tests/csapi/... -run TestRoomCreate
# To run a specific test, you can specify the whole name structure:
scripts-dev/complement.sh ./tests/csapi/... -run TestRoomCreate/Parallel/POST_/createRoom_makes_a_public_room
# Generally though, the `-run` parameter accepts regex patterns, so you can match however you like:
scripts-dev/complement.sh ./tests/... -run 'TestRoomCreate/Parallel/POST_/createRoom_makes_a_(.*)'
```

Typically, if you're developing the Synapse and Complement tests side-by-side, you will
run something like this:

```shell
# To run a specific test
COMPLEMENT_DIR=../complement ./scripts-dev/complement.sh ./tests/csapi/... -run TestRoomCreate
```


### Running in-repo tests

In-repo Complement tests are tests that are vendored into this project. We use the
in-repo test suite to test Synapse specific behaviors like the admin API.

To run the in-repo Complement tests, use the `--in-repo` command line argument.

```shell
# Run only a specific test package.
# Note: test packages are relative to the `./complement` directory in this project
./scripts-dev/complement.sh --in-repo ./tests/...

# Similarly, you can also use `-run` to specify all or part of a specific test path to run
scripts-dev/complement.sh --in-repo ./tests/... -run TestIntraShardFederation
```
