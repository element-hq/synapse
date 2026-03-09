# Complement testing

Complement is a black box integration testing framework for Matrix homeservers. It
allows us to write end-to-end tests that interact with real Synapse homeservers to
ensure everything works at a holistic level.


## Setup

Nothing beyond a [normal Complement
setup](https://github.com/matrix-org/complement#running) (just Go and Docker).


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

It's often nice to develop on Synapse and write Complement tests at the same time.
Here is how to run your local Synapse checkout against your local Complement checkout.

```shell
# To run a specific test
COMPLEMENT_DIR=../complement ./scripts-dev/complement.sh ./tests/csapi/... -run TestRoomCreate
```

The above will run a monolithic (single-process) Synapse with SQLite as the database.
For other configurations, try:

- Passing `POSTGRES=1` as an environment variable to use the Postgres database instead.
- Passing `WORKERS=1` as an environment variable to use a workerised setup instead. This
  option implies the use of Postgres.
  - If setting `WORKERS=1`, optionally set `WORKER_TYPES=` to declare which worker types
    you wish to test. A simple comma-delimited string containing the worker types
    defined from the `WORKERS_CONFIG` template in
    [here](https://github.com/element-hq/synapse/blob/develop/docker/configure_workers_and_start.py#L54).
    A safe example would be `WORKER_TYPES="federation_inbound, federation_sender,
    synchrotron"`. See the [worker documentation](../workers.md) for additional
    information on workers.
- Passing `ASYNCIO_REACTOR=1` as an environment variable to use the asyncio-backed
  reactor with Twisted instead of the default one.
- Passing `PODMAN=1` will use the [podman](https://podman.io/) container runtime,
  instead of docker.
- Passing `UNIX_SOCKETS=1` will utilise Unix socket functionality for Synapse, Redis,
  and Postgres(when applicable).

To increase the log level for the tests, set `SYNAPSE_TEST_LOG_LEVEL`, e.g:
```sh
SYNAPSE_TEST_LOG_LEVEL=DEBUG COMPLEMENT_DIR=../complement ./scripts-dev/complement.sh -run TestRoomCreate
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

### Access database for homeserver after Complement test runs.

If you're curious what the database looks like after you run some tests, here are some
steps to get you going in Synapse:

1. In your Complement test comment out `defer deployment.Destroy(t)` and replace with
   `defer time.Sleep(2 * time.Hour)` to keep the homeserver running after the tests
   complete
1. Start the Complement tests
1. Find the name of the container, `docker ps -f name=complement_` (this will filter for
   just the Complement related Docker containers)
1. Access the container replacing the name with what you found in the previous step:
   `docker exec -it complement_1_hs_with_application_service.hs1_2 /bin/bash`
1. Install sqlite (database driver), `apt-get update && apt-get install -y sqlite3`
1. Then run `sqlite3` and open the database `.open /conf/homeserver.db` (this db path
   comes from the Synapse homeserver.yaml)
