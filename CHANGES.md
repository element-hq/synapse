# Synapse 1.135.0rc2 (2025-07-30)

### Bugfixes

- Fix user failing to deactivate with MAS when `/_synapse/mas` is handled by a worker. ([\#18716](https://github.com/element-hq/synapse/issues/18716))

### Internal Changes

- Fix performance regression introduced in [#18238](https://github.com/element-hq/synapse/issues/18238) by adding a cache to `is_server_admin`. ([\#18747](https://github.com/element-hq/synapse/issues/18747))




# Synapse 1.135.0rc1 (2025-07-22)

### Features

- Add `recaptcha_private_key_path` and `recaptcha_public_key_path` config option. ([\#17984](https://github.com/element-hq/synapse/issues/17984), [\#18684](https://github.com/element-hq/synapse/issues/18684))
- Add plain-text handling for rich-text topics as per [MSC3765](https://github.com/matrix-org/matrix-spec-proposals/pull/3765). ([\#18195](https://github.com/element-hq/synapse/issues/18195))
- If enabled by the user, server admins will see [soft failed](https://spec.matrix.org/v1.13/server-server-api/#soft-failure) events over the Client-Server API. ([\#18238](https://github.com/element-hq/synapse/issues/18238))
- Add experimental support for [MSC4277: Harmonizing the reporting endpoints](https://github.com/matrix-org/matrix-spec-proposals/pull/4277). ([\#18263](https://github.com/element-hq/synapse/issues/18263))
- Add ability to limit amount of media uploaded by a user in a given time period. ([\#18527](https://github.com/element-hq/synapse/issues/18527))
- Enable workers to write directly to the device lists stream and handle device list updates, reducing load on the main process. ([\#18581](https://github.com/element-hq/synapse/issues/18581))
- Support arbitrary profile fields. Contributed by @clokep. ([\#18635](https://github.com/element-hq/synapse/issues/18635))
- Advertise support for Matrix v1.12. ([\#18647](https://github.com/element-hq/synapse/issues/18647))
- Add an option to issue redactions as an admin user via the [admin redaction endpoint](https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#redact-all-the-events-of-a-user). ([\#18671](https://github.com/element-hq/synapse/issues/18671))
- Add experimental and incomplete support for [MSC4306: Thread Subscriptions](https://github.com/matrix-org/matrix-spec-proposals/blob/rei/msc_thread_subscriptions/proposals/4306-thread-subscriptions.md). ([\#18674](https://github.com/element-hq/synapse/issues/18674))
- Include `event_id` when getting state with `?format=event`. Contributed by @tulir @ Beeper. ([\#18675](https://github.com/element-hq/synapse/issues/18675))

### Bugfixes

- Fix CPU and database spinning when retrying sending events to servers whilst at the same time purging those events. ([\#18499](https://github.com/element-hq/synapse/issues/18499))
- Don't allow creation of tags with names longer than 255 bytes, [as per the spec](https://spec.matrix.org/v1.15/client-server-api/#events-14). ([\#18660](https://github.com/element-hq/synapse/issues/18660))
- Fix `sliding_sync_connections`-related errors when porting from SQLite to Postgres. ([\#18677](https://github.com/element-hq/synapse/issues/18677))
- Fix the MAS integration not working when Synapse is started with `--daemonize` or using `synctl`. ([\#18691](https://github.com/element-hq/synapse/issues/18691))

### Improved Documentation

- Document that some config options for the user directory are in violation of the Matrix spec. ([\#18548](https://github.com/element-hq/synapse/issues/18548))
- Update `rc_delayed_event_mgmt` docs to the actual nesting level. Contributed by @HarHarLinks. ([\#18692](https://github.com/element-hq/synapse/issues/18692))

### Internal Changes

- Add a dedicated internal API for Matrix Authentication Service to Synapse communication. ([\#18520](https://github.com/element-hq/synapse/issues/18520))
- Allow user registrations to be done on workers. ([\#18552](https://github.com/element-hq/synapse/issues/18552))
- Remove unnecessary HTTP replication calls. ([\#18564](https://github.com/element-hq/synapse/issues/18564))
- Refactor `Measure` block metrics to be homeserver-scoped. ([\#18601](https://github.com/element-hq/synapse/issues/18601))
- Refactor cache metrics to be homeserver-scoped. ([\#18604](https://github.com/element-hq/synapse/issues/18604))
- Unbreak "Latest dependencies" workflow by using the `--without dev` poetry option instead of removed `--no-dev`. ([\#18617](https://github.com/element-hq/synapse/issues/18617))
- Update URL Preview code to work with `lxml` 6.0.0+. ([\#18622](https://github.com/element-hq/synapse/issues/18622))
- Use `markdown-it-py` instead of `commonmark` in the release script. ([\#18637](https://github.com/element-hq/synapse/issues/18637))
- Fix typing errors with upgraded mypy version. ([\#18653](https://github.com/element-hq/synapse/issues/18653))
- Add doc comment explaining that config files are shallowly merged. ([\#18664](https://github.com/element-hq/synapse/issues/18664))
- Minor speed up of insertion into `stream_positions` table. ([\#18672](https://github.com/element-hq/synapse/issues/18672))
- Remove unused `allow_no_prev_events` option when creating an event. ([\#18676](https://github.com/element-hq/synapse/issues/18676))
- Clean up `MetricsResource` and Prometheus hacks. ([\#18687](https://github.com/element-hq/synapse/issues/18687))
- Fix dirty `Cargo.lock` changes appearing after install (`base64`). ([\#18689](https://github.com/element-hq/synapse/issues/18689))
- Prevent dirty `Cargo.lock` changes from install. ([\#18693](https://github.com/element-hq/synapse/issues/18693))
- Correct spelling of 'Admin token used' log line. ([\#18697](https://github.com/element-hq/synapse/issues/18697))
- Reduce log spam when client stops downloading media while it is being streamed to them. ([\#18699](https://github.com/element-hq/synapse/issues/18699))



### Updates to locked dependencies

* Bump authlib from 1.6.0 to 1.6.1. ([\#18704](https://github.com/element-hq/synapse/issues/18704))
* Bump base64 from 0.21.7 to 0.22.1. ([\#18666](https://github.com/element-hq/synapse/issues/18666))
* Bump jsonschema from 4.24.0 to 4.25.0. ([\#18707](https://github.com/element-hq/synapse/issues/18707))
* Bump lxml from 5.4.0 to 6.0.0. ([\#18631](https://github.com/element-hq/synapse/issues/18631))
* Bump mypy from 1.13.0 to 1.16.1. ([\#18653](https://github.com/element-hq/synapse/issues/18653))
* Bump once_cell from 1.19.0 to 1.21.3. ([\#18710](https://github.com/element-hq/synapse/issues/18710))
* Bump phonenumbers from 9.0.8 to 9.0.9. ([\#18681](https://github.com/element-hq/synapse/issues/18681))
* Bump ruff from 0.12.2 to 0.12.5. ([\#18683](https://github.com/element-hq/synapse/issues/18683), [\#18705](https://github.com/element-hq/synapse/issues/18705))
* Bump serde_json from 1.0.140 to 1.0.141. ([\#18709](https://github.com/element-hq/synapse/issues/18709))
* Bump sigstore/cosign-installer from 3.9.1 to 3.9.2. ([\#18708](https://github.com/element-hq/synapse/issues/18708))
* Bump types-jsonschema from 4.24.0.20250528 to 4.24.0.20250708. ([\#18682](https://github.com/element-hq/synapse/issues/18682))

# Synapse 1.134.0 (2025-07-15)

No significant changes since 1.134.0rc1.




# Synapse 1.134.0rc1 (2025-07-09)

### Features

- Support for [MSC4235](https://github.com/matrix-org/matrix-spec-proposals/pull/4235): `via` query param for hierarchy endpoint. Contributed by Krishan (@kfiven). ([\#18070](https://github.com/element-hq/synapse/issues/18070))
- Add `forget_forced_upon_leave` capability as per [MSC4267](https://github.com/matrix-org/matrix-spec-proposals/pull/4267). ([\#18196](https://github.com/element-hq/synapse/issues/18196))
- Add `federated_user_may_invite` spam checker callback which receives the entire invite event. Contributed by @tulir @ Beeper. ([\#18241](https://github.com/element-hq/synapse/issues/18241))

### Bugfixes

- Fix `KeyError` on background updates when using split main/state databases. ([\#18509](https://github.com/element-hq/synapse/issues/18509))
- Improve performance of device deletion by adding missing index. ([\#18582](https://github.com/element-hq/synapse/issues/18582))
- Fix `avatar_url` and `displayname` being sent on federation profile queries when they are not set. ([\#18593](https://github.com/element-hq/synapse/issues/18593))
- Respond with 401 & `M_USER_LOCKED` when a locked user calls `POST /login`, as per the spec. ([\#18594](https://github.com/element-hq/synapse/issues/18594))
- Ensure policy servers are not asked to scan policy server change events, allowing rooms to disable the use of a policy server while the policy server is down. ([\#18605](https://github.com/element-hq/synapse/issues/18605))

### Improved Documentation

- Fix documentation of the Delete Room Admin API's status field. ([\#18519](https://github.com/element-hq/synapse/issues/18519))

### Deprecations and Removals

- Stop adding the "origin" field to newly-created events (PDUs). ([\#18418](https://github.com/element-hq/synapse/issues/18418))

### Internal Changes

- Replace `PyICU` crate with equivalent `icu_segmenter` Rust crate. ([\#18553](https://github.com/element-hq/synapse/issues/18553), [\#18646](https://github.com/element-hq/synapse/issues/18646))
- Improve docstring on `simple_upsert_many`. ([\#18573](https://github.com/element-hq/synapse/issues/18573))
- Raise poetry-core version cap to 2.1.3. ([\#18575](https://github.com/element-hq/synapse/issues/18575))
- Raise setuptools_rust version cap to 1.11.1. ([\#18576](https://github.com/element-hq/synapse/issues/18576))
- Better handling of ratelimited requests. ([\#18595](https://github.com/element-hq/synapse/issues/18595), [\#18600](https://github.com/element-hq/synapse/issues/18600))
- Update to Rust 1.87.0 in CI, and bump the pinned commit of the `dtolnay/rust-toolchain` GitHub Action to `b3b07ba8b418998c39fb20f53e8b695cdcc8de1b`. ([\#18596](https://github.com/element-hq/synapse/issues/18596))
- Speed up bulk device deletion. ([\#18602](https://github.com/element-hq/synapse/issues/18602))
- Speed up the building of arm-based wheels in CI. ([\#18618](https://github.com/element-hq/synapse/issues/18618))
- Speed up the building of Docker images in CI. ([\#18620](https://github.com/element-hq/synapse/issues/18620))
- Add `.zed/` directory to `.gitignore`. ([\#18623](https://github.com/element-hq/synapse/issues/18623))
- Log the room ID we're purging state for. ([\#18625](https://github.com/element-hq/synapse/issues/18625))



### Updates to locked dependencies

* Bump Swatinem/rust-cache from 2.7.8 to 2.8.0. ([\#18612](https://github.com/element-hq/synapse/issues/18612))
* Bump attrs from 24.2.0 to 25.3.0. ([\#18649](https://github.com/element-hq/synapse/issues/18649))
* Bump authlib from 1.5.2 to 1.6.0. ([\#18642](https://github.com/element-hq/synapse/issues/18642))
* Bump base64 from 0.21.7 to 0.22.1. ([\#18589](https://github.com/element-hq/synapse/issues/18589))
* Bump base64 from 0.21.7 to 0.22.1. ([\#18629](https://github.com/element-hq/synapse/issues/18629))
* Bump docker/build-push-action from 6.17.0 to 6.18.0. ([\#18497](https://github.com/element-hq/synapse/issues/18497))
* Bump docker/setup-buildx-action from 3.10.0 to 3.11.1. ([\#18587](https://github.com/element-hq/synapse/issues/18587))
* Bump hiredis from 3.1.0 to 3.2.1. ([\#18638](https://github.com/element-hq/synapse/issues/18638))
* Bump ijson from 3.3.0 to 3.4.0. ([\#18650](https://github.com/element-hq/synapse/issues/18650))
* Bump jsonschema from 4.23.0 to 4.24.0. ([\#18630](https://github.com/element-hq/synapse/issues/18630))
* Bump msgpack from 1.1.0 to 1.1.1. ([\#18651](https://github.com/element-hq/synapse/issues/18651))
* Bump mypy-zope from 1.0.11 to 1.0.12. ([\#18640](https://github.com/element-hq/synapse/issues/18640))
* Bump phonenumbers from 9.0.2 to 9.0.8. ([\#18652](https://github.com/element-hq/synapse/issues/18652))
* Bump pillow from 11.2.1 to 11.3.0. ([\#18624](https://github.com/element-hq/synapse/issues/18624))
* Bump prometheus-client from 0.21.0 to 0.22.1. ([\#18609](https://github.com/element-hq/synapse/issues/18609))
* Bump pyasn1-modules from 0.4.1 to 0.4.2. ([\#18495](https://github.com/element-hq/synapse/issues/18495))
* Bump pydantic from 2.11.4 to 2.11.7. ([\#18639](https://github.com/element-hq/synapse/issues/18639))
* Bump reqwest from 0.12.15 to 0.12.20. ([\#18590](https://github.com/element-hq/synapse/issues/18590))
* Bump reqwest from 0.12.20 to 0.12.22. ([\#18627](https://github.com/element-hq/synapse/issues/18627))
* Bump ruff from 0.11.11 to 0.12.1. ([\#18645](https://github.com/element-hq/synapse/issues/18645))
* Bump ruff from 0.12.1 to 0.12.2. ([\#18657](https://github.com/element-hq/synapse/issues/18657))
* Bump sentry-sdk from 2.22.0 to 2.32.0. ([\#18633](https://github.com/element-hq/synapse/issues/18633))
* Bump setuptools-rust from 1.10.2 to 1.11.1. ([\#18655](https://github.com/element-hq/synapse/issues/18655))
* Bump sigstore/cosign-installer from 3.8.2 to 3.9.0. ([\#18588](https://github.com/element-hq/synapse/issues/18588))
* Bump sigstore/cosign-installer from 3.9.0 to 3.9.1. ([\#18608](https://github.com/element-hq/synapse/issues/18608))
* Bump stefanzweifel/git-auto-commit-action from 5.2.0 to 6.0.1. ([\#18607](https://github.com/element-hq/synapse/issues/18607))
* Bump tokio from 1.45.1 to 1.46.0. ([\#18628](https://github.com/element-hq/synapse/issues/18628))
* Bump tokio from 1.46.0 to 1.46.1. ([\#18667](https://github.com/element-hq/synapse/issues/18667))
* Bump treq from 24.9.1 to 25.5.0. ([\#18610](https://github.com/element-hq/synapse/issues/18610))
* Bump types-bleach from 6.2.0.20241123 to 6.2.0.20250514. ([\#18634](https://github.com/element-hq/synapse/issues/18634))
* Bump types-jsonschema from 4.23.0.20250516 to 4.24.0.20250528. ([\#18611](https://github.com/element-hq/synapse/issues/18611))
* Bump types-opentracing from 2.4.10.6 to 2.4.10.20250622. ([\#18586](https://github.com/element-hq/synapse/issues/18586))
* Bump types-psycopg2 from 2.9.21.20250318 to 2.9.21.20250516. ([\#18658](https://github.com/element-hq/synapse/issues/18658))
* Bump types-pyyaml from 6.0.12.20241230 to 6.0.12.20250516. ([\#18643](https://github.com/element-hq/synapse/issues/18643))
* Bump types-setuptools from 75.2.0.20241019 to 80.9.0.20250529. ([\#18644](https://github.com/element-hq/synapse/issues/18644))
* Bump typing-extensions from 4.12.2 to 4.14.0. ([\#18654](https://github.com/element-hq/synapse/issues/18654))
* Bump typing-extensions from 4.14.0 to 4.14.1. ([\#18668](https://github.com/element-hq/synapse/issues/18668))
* Bump urllib3 from 2.2.2 to 2.5.0. ([\#18572](https://github.com/element-hq/synapse/issues/18572))

# Synapse 1.133.0 (2025-07-01)

Pre-built wheels are now built using the [manylinux_2_28](https://github.com/pypa/manylinux#manylinux_2_28-almalinux-8-based) base, which is expected to be compatible with distros using glibc 2.28 or later, including:

 - Debian 10+
 - Ubuntu 18.10+
 - Fedora 29+
 - CentOS/RHEL 8+

Previously, wheels were built using the [manylinux2014](https://github.com/pypa/manylinux#manylinux2014-centos-7-based-glibc-217) base, which was expected to be compatible with distros using glibc 2.17 or later.

### Bugfixes

- Bump `cibuildwheel` to 3.0.0 to fix the `manylinux` wheel builds. ([\#18615](https://github.com/element-hq/synapse/issues/18615))




# Synapse 1.133.0rc1 (2025-06-24)

### Features

- Add support for the [MSC4260 user report API](https://github.com/matrix-org/matrix-spec-proposals/pull/4260). ([\#18120](https://github.com/element-hq/synapse/issues/18120))

### Bugfixes

- Fix an issue where, during state resolution for v11 rooms, Synapse would incorrectly calculate the power level of the creator when there was no power levels event in the room. ([\#18534](https://github.com/element-hq/synapse/issues/18534), [\#18547](https://github.com/element-hq/synapse/issues/18547))
- Fix long-standing bug where sliding sync did not honour the `room_id_to_include` config option. ([\#18535](https://github.com/element-hq/synapse/issues/18535))
- Fix an issue where "Lock timeout is getting excessive" warnings would be logged even when the lock timeout was <10 minutes. ([\#18543](https://github.com/element-hq/synapse/issues/18543))
- Fix an issue where Synapse could calculate the wrong power level for the creator of the room if there was no power levels event. ([\#18545](https://github.com/element-hq/synapse/issues/18545))

### Improved Documentation

- Generate config documentation from JSON Schema file. ([\#18528](https://github.com/element-hq/synapse/issues/18528))
- Fix typo in user type documentation. ([\#18568](https://github.com/element-hq/synapse/issues/18568))

### Internal Changes

- Increase performance of introspecting access tokens when using delegated auth. ([\#18357](https://github.com/element-hq/synapse/issues/18357), [\#18561](https://github.com/element-hq/synapse/issues/18561))
- Log user deactivations. ([\#18541](https://github.com/element-hq/synapse/issues/18541))
- Enable [`flake8-logging`](https://docs.astral.sh/ruff/rules/#flake8-logging-log) and [`flake8-logging-format`](https://docs.astral.sh/ruff/rules/#flake8-logging-format-g) rules in Ruff and fix related issues throughout the codebase. ([\#18542](https://github.com/element-hq/synapse/issues/18542))
- Clean up old, unused rows from the `device_federation_inbox` table. ([\#18546](https://github.com/element-hq/synapse/issues/18546))
- Run config schema CI on develop and release branches. ([\#18551](https://github.com/element-hq/synapse/issues/18551))
- Add support for Twisted `25.5.0`+ releases. ([\#18577](https://github.com/element-hq/synapse/issues/18577))
- Update PyO3 to version 0.25. ([\#18578](https://github.com/element-hq/synapse/issues/18578))



### Updates to locked dependencies

* Bump actions/setup-python from 5.5.0 to 5.6.0. ([\#18555](https://github.com/element-hq/synapse/issues/18555))
* Bump base64 from 0.21.7 to 0.22.1. ([\#18559](https://github.com/element-hq/synapse/issues/18559))
* Bump dawidd6/action-download-artifact from 9 to 11. ([\#18556](https://github.com/element-hq/synapse/issues/18556))
* Bump headers from 0.4.0 to 0.4.1. ([\#18529](https://github.com/element-hq/synapse/issues/18529))
* Bump requests from 2.32.2 to 2.32.4. ([\#18533](https://github.com/element-hq/synapse/issues/18533))
* Bump types-requests from 2.32.0.20250328 to 2.32.4.20250611. ([\#18558](https://github.com/element-hq/synapse/issues/18558))

# Synapse 1.132.0 (2025-06-17)

### Improved Documentation

- Improvements to generate config documentation from JSON Schema file. ([\#18522](https://github.com/element-hq/synapse/issues/18522))




# Synapse 1.132.0rc1 (2025-06-10)

### Features

- Add support for [MSC4155](https://github.com/matrix-org/matrix-spec-proposals/pull/4155) Invite Filtering. ([\#18288](https://github.com/element-hq/synapse/issues/18288))
- Add experimental `user_may_send_state_event` module API callback. ([\#18455](https://github.com/element-hq/synapse/issues/18455))
- Add experimental `get_media_config_for_user` and `is_user_allowed_to_upload_media_of_size` module API callbacks that allow overriding of media repository maximum upload size. ([\#18457](https://github.com/element-hq/synapse/issues/18457))
- Add experimental `get_ratelimit_override_for_user` module API callback that allows overriding of per-user ratelimits. ([\#18458](https://github.com/element-hq/synapse/issues/18458))
- Pass `room_config` argument to `user_may_create_room` spam checker module callback. ([\#18486](https://github.com/element-hq/synapse/issues/18486))
- Support configuration of default and extra user types. ([\#18456](https://github.com/element-hq/synapse/issues/18456))
- Successful requests to `/_matrix/app/v1/ping` will now force Synapse to reattempt delivering transactions to appservices. ([\#18521](https://github.com/element-hq/synapse/issues/18521))
- Support the import of the `RatelimitOverride` type from `synapse.module_api` in modules and rename `messages_per_second` to `per_second`. ([\#18513](https://github.com/element-hq/synapse/issues/18513))

### Bugfixes

- Remove destinations from sending if not whitelisted. ([\#18484](https://github.com/element-hq/synapse/issues/18484))
- Fixed room summary API incorrectly returning that a room is private in the room summary response when the join rule is omitted by the remote server. Contributed by @nexy7574. ([\#18493](https://github.com/element-hq/synapse/issues/18493))
- Prevent users from adding themselves to their own user ignore list. ([\#18508](https://github.com/element-hq/synapse/issues/18508))

### Improved Documentation

- Generate config documentation from JSON Schema file. ([\#17892](https://github.com/element-hq/synapse/issues/17892))
- Mention `CAP_NET_BIND_SERVICE` as an alternative to running Synapse as root in order to bind to a privileged port. ([\#18408](https://github.com/element-hq/synapse/issues/18408))
- Surface hidden Admin API documentation regarding fetching of scheduled tasks. ([\#18516](https://github.com/element-hq/synapse/issues/18516))
- Mark the new module APIs in this release as experimental. ([\#18536](https://github.com/element-hq/synapse/issues/18536))

### Internal Changes

- Mark dehydrated devices in the [List All User Devices Admin API](https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#list-all-devices). ([\#18252](https://github.com/element-hq/synapse/issues/18252))
- Reduce disk wastage by cleaning up `received_transactions` older than 1 day, rather than 30 days. ([\#18310](https://github.com/element-hq/synapse/issues/18310))
- Distinguish all vs local events being persisted in the "Event Send Time Quantiles" graph (Grafana). ([\#18510](https://github.com/element-hq/synapse/issues/18510))




# Synapse 1.131.0 (2025-06-03)

No significant changes since 1.131.0rc1.

# Synapse 1.131.0rc1 (2025-05-28)

### Features

- Add `msc4263_limit_key_queries_to_users_who_share_rooms` config option as per [MSC4263](https://github.com/matrix-org/matrix-spec-proposals/pull/4263). ([\#18180](https://github.com/element-hq/synapse/issues/18180))
- Add option to allow registrations that begin with `_`. Contributed by `_` (@hex5f). ([\#18262](https://github.com/element-hq/synapse/issues/18262))
- Include room ID in response to the [Room Deletion Status Admin API](https://element-hq.github.io/synapse/latest/admin_api/rooms.html#status-of-deleting-rooms). ([\#18318](https://github.com/element-hq/synapse/issues/18318))
- Add support for calling Policy Servers ([MSC4284](https://github.com/matrix-org/matrix-spec-proposals/pull/4284)) to mark events as spam. ([\#18387](https://github.com/element-hq/synapse/issues/18387))

### Bugfixes

- Prevent race-condition in `_maybe_retry_device_resync` entrance. ([\#18391](https://github.com/element-hq/synapse/issues/18391))
- Fix the `tests.handlers.test_worker_lock.WorkerLockTestCase.test_lock_contention` test which could spuriously time out on RISC-V architectures due to performance differences. ([\#18430](https://github.com/element-hq/synapse/issues/18430))
- Fix admin redaction endpoint not redacting encrypted messages. ([\#18434](https://github.com/element-hq/synapse/issues/18434))

### Improved Documentation

- Update `room_list_publication_rules` docs to consider defaults that changed in v1.126.0. Contributed by @HarHarLinks. ([\#18286](https://github.com/element-hq/synapse/issues/18286))
- Add advice for upgrading between major PostgreSQL versions to the database documentation. ([\#18445](https://github.com/element-hq/synapse/issues/18445))

### Internal Changes

- Fix a memory leak in `_NotifierUserStream`. ([\#18380](https://github.com/element-hq/synapse/issues/18380))
- Fix a couple type annotations in the `RootConfig`/`Config`. ([\#18409](https://github.com/element-hq/synapse/issues/18409))
- Explicitly enable PyPy builds in `cibuildwheel`s config to avoid it being disabled on a future upgrade to `cibuildwheel` v3. ([\#18417](https://github.com/element-hq/synapse/issues/18417))
- Update the PR review template to remove an erroneous line break from the final bullet point. ([\#18419](https://github.com/element-hq/synapse/issues/18419))
- Explain why we `flush_buffer()` for Python `print(...)` output. ([\#18420](https://github.com/element-hq/synapse/issues/18420))
- Add lint to ensure we don't add a `CREATE/DROP INDEX` in a schema delta. ([\#18440](https://github.com/element-hq/synapse/issues/18440))
- Allow checking only for the existence of a field in an SSO provider's response, rather than requiring the value(s) to check. ([\#18454](https://github.com/element-hq/synapse/issues/18454))
- Add unit tests for homeserver usage statistics. ([\#18463](https://github.com/element-hq/synapse/issues/18463))
- Don't move invited users to new room when shutting down room. ([\#18471](https://github.com/element-hq/synapse/issues/18471))



### Updates to locked dependencies

* Bump actions/setup-python from 5.5.0 to 5.6.0. ([\#18398](https://github.com/element-hq/synapse/issues/18398))
* Bump authlib from 1.5.1 to 1.5.2. ([\#18452](https://github.com/element-hq/synapse/issues/18452))
* Bump docker/build-push-action from 6.15.0 to 6.17.0. ([\#18397](https://github.com/element-hq/synapse/issues/18397), [\#18449](https://github.com/element-hq/synapse/issues/18449))
* Bump lxml from 5.3.0 to 5.4.0. ([\#18480](https://github.com/element-hq/synapse/issues/18480))
* Bump mypy-zope from 1.0.9 to 1.0.11. ([\#18428](https://github.com/element-hq/synapse/issues/18428))
* Bump pyo3 from 0.23.5 to 0.24.2. ([\#18460](https://github.com/element-hq/synapse/issues/18460))
* Bump pyo3-log from 0.12.3 to 0.12.4. ([\#18453](https://github.com/element-hq/synapse/issues/18453))
* Bump pyopenssl from 25.0.0 to 25.1.0. ([\#18450](https://github.com/element-hq/synapse/issues/18450))
* Bump ruff from 0.7.3 to 0.11.11. ([\#18451](https://github.com/element-hq/synapse/issues/18451), [\#18482](https://github.com/element-hq/synapse/issues/18482))
* Bump tornado from 6.4.2 to 6.5.0. ([\#18459](https://github.com/element-hq/synapse/issues/18459))
* Bump setuptools from 72.1.0 to 78.1.1. ([\#18461](https://github.com/element-hq/synapse/issues/18461))
* Bump types-jsonschema from 4.23.0.20241208 to 4.23.0.20250516. ([\#18481](https://github.com/element-hq/synapse/issues/18481))
* Bump types-requests from 2.32.0.20241016 to 2.32.0.20250328. ([\#18427](https://github.com/element-hq/synapse/issues/18427))

# Synapse 1.130.0 (2025-05-20)

### Bugfixes

- Fix startup being blocked on creating a new index that was introduced in v1.130.0rc1. ([\#18439](https://github.com/element-hq/synapse/issues/18439))
- Fix the ordering of local messages in rooms that were affected by [GHSA-v56r-hwv5-mxg6](https://github.com/advisories/GHSA-v56r-hwv5-mxg6). ([\#18447](https://github.com/element-hq/synapse/issues/18447))




# Synapse 1.130.0rc1 (2025-05-13)

### Features

- Add an Admin API endpoint `GET /_synapse/admin/v1/scheduled_tasks`  to fetch scheduled tasks. ([\#18214](https://github.com/element-hq/synapse/issues/18214))
- Add config option `user_directory.exclude_remote_users` which, when enabled, excludes remote users from user directory search results. ([\#18300](https://github.com/element-hq/synapse/issues/18300))
- Add support for handling `GET /devices/` on workers. ([\#18355](https://github.com/element-hq/synapse/issues/18355))

### Bugfixes

- Fix a longstanding bug where Synapse would immediately retry a failing push endpoint when a new event is received, ignoring any backoff timers. ([\#18363](https://github.com/element-hq/synapse/issues/18363))
- Pass leave from remote invite rejection down Sliding Sync. ([\#18375](https://github.com/element-hq/synapse/issues/18375))

### Updates to the Docker image

- In `configure_workers_and_start.py`, use the same absolute path of Python in the interpreter shebang, and invoke child Python processes with `sys.executable`. ([\#18291](https://github.com/element-hq/synapse/issues/18291))
- Optimize the build of the workers image. ([\#18292](https://github.com/element-hq/synapse/issues/18292))
- In `start_for_complement.sh`, replace some external program calls with shell builtins. ([\#18293](https://github.com/element-hq/synapse/issues/18293))
- When generating container scripts from templates, don't add a leading newline so that their shebangs may be handled correctly. ([\#18295](https://github.com/element-hq/synapse/issues/18295))

### Improved Documentation

- Improve formatting of the README file. ([\#18218](https://github.com/element-hq/synapse/issues/18218))
- Add documentation for configuring [Pocket ID](https://github.com/pocket-id/pocket-id) as an OIDC provider. ([\#18237](https://github.com/element-hq/synapse/issues/18237))
- Fix typo in docs about the `push` config option. Contributed by @HarHarLinks. ([\#18320](https://github.com/element-hq/synapse/issues/18320))
- Add `/_matrix/federation/v1/version` to list of federation endpoints that can be handled by workers. ([\#18377](https://github.com/element-hq/synapse/issues/18377))
- Add an Admin API endpoint `GET /_synapse/admin/v1/scheduled_tasks`  to fetch scheduled tasks. ([\#18384](https://github.com/element-hq/synapse/issues/18384))

### Internal Changes

- Return specific error code when adding an email address / phone number to account is not supported ([MSC4178](https://github.com/matrix-org/matrix-spec-proposals/pull/4178)). ([\#17578](https://github.com/element-hq/synapse/issues/17578))
- Stop auto-provisionning missing users & devices when delegating auth to Matrix Authentication Service. Requires MAS 0.13.0 or later. ([\#18181](https://github.com/element-hq/synapse/issues/18181))
- Apply file hashing and existing quarantines to media downloaded for URL previews. ([\#18297](https://github.com/element-hq/synapse/issues/18297))
- Allow a few admin APIs used by matrix-authentication-service to run on workers. ([\#18313](https://github.com/element-hq/synapse/issues/18313))
- Apply `should_drop_federated_event` to federation invites. ([\#18330](https://github.com/element-hq/synapse/issues/18330))
- Allow `/rooms/` admin API to be run on workers. ([\#18360](https://github.com/element-hq/synapse/issues/18360))
- Minor performance improvements to the notifier. ([\#18367](https://github.com/element-hq/synapse/issues/18367))
- Slight performance increase when using the ratelimiter. ([\#18369](https://github.com/element-hq/synapse/issues/18369))
- Don't validate the `at_hash` (access token hash) field in OIDC ID Tokens if we don't end up actually using the OIDC Access Token. ([\#18374](https://github.com/element-hq/synapse/issues/18374), [\#18385](https://github.com/element-hq/synapse/issues/18385))
- Fixed test failures when using authlib 1.5.2. ([\#18390](https://github.com/element-hq/synapse/issues/18390))
- Refactor [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) Simplified Sliding Sync room list tests to cover both new and fallback logic paths. ([\#18399](https://github.com/element-hq/synapse/issues/18399))



### Updates to locked dependencies

* Bump actions/add-to-project from 280af8ae1f83a494cfad2cb10f02f6d13529caa9 to 5b1a254a3546aef88e0a7724a77a623fa2e47c36. ([\#18365](https://github.com/element-hq/synapse/issues/18365))
* Bump actions/download-artifact from 4.2.1 to 4.3.0. ([\#18364](https://github.com/element-hq/synapse/issues/18364))
* Bump actions/setup-go from 5.4.0 to 5.5.0. ([\#18426](https://github.com/element-hq/synapse/issues/18426))
* Bump anyhow from 1.0.97 to 1.0.98. ([\#18336](https://github.com/element-hq/synapse/issues/18336))
* Bump packaging from 24.2 to 25.0. ([\#18393](https://github.com/element-hq/synapse/issues/18393))
* Bump pillow from 11.1.0 to 11.2.1. ([\#18429](https://github.com/element-hq/synapse/issues/18429))
* Bump pydantic from 2.10.3 to 2.11.4. ([\#18394](https://github.com/element-hq/synapse/issues/18394))
* Bump pyo3-log from 0.12.2 to 0.12.3. ([\#18317](https://github.com/element-hq/synapse/issues/18317))
* Bump pyopenssl from 24.3.0 to 25.0.0. ([\#18315](https://github.com/element-hq/synapse/issues/18315))
* Bump sha2 from 0.10.8 to 0.10.9. ([\#18395](https://github.com/element-hq/synapse/issues/18395))
* Bump sigstore/cosign-installer from 3.8.1 to 3.8.2. ([\#18366](https://github.com/element-hq/synapse/issues/18366))
* Bump softprops/action-gh-release from 1 to 2. ([\#18264](https://github.com/element-hq/synapse/issues/18264))
* Bump stefanzweifel/git-auto-commit-action from 5.1.0 to 5.2.0. ([\#18354](https://github.com/element-hq/synapse/issues/18354))
* Bump txredisapi from 1.4.10 to 1.4.11. ([\#18392](https://github.com/element-hq/synapse/issues/18392))
* Bump types-jsonschema from 4.23.0.20240813 to 4.23.0.20241208. ([\#18305](https://github.com/element-hq/synapse/issues/18305))
* Bump types-psycopg2 from 2.9.21.20250121 to 2.9.21.20250318. ([\#18316](https://github.com/element-hq/synapse/issues/18316))

# Synapse 1.129.0 (2025-05-06)

No significant changes since 1.129.0rc2.




# Synapse 1.129.0rc2 (2025-04-30)

Synapse 1.129.0rc1 was never formally released due to regressions discovered during the release process. 1.129.0rc2 fixes those regressions by reverting the affected PRs.

### Internal Changes

- Revert the slow background update introduced by [\#18068](https://github.com/element-hq/synapse/issues/18068) in v1.128.0. ([\#18372](https://github.com/element-hq/synapse/issues/18372))
- Revert "Add total event, unencrypted message, and e2ee event counts to stats reporting", added in v1.129.0rc1. ([\#18373](https://github.com/element-hq/synapse/issues/18373))




# Synapse 1.129.0rc1 (2025-04-15)

### Features

- Add `passthrough_authorization_parameters` in OIDC configuration to allow passing parameters to the authorization grant URL. ([\#18232](https://github.com/element-hq/synapse/issues/18232))
- Add `total_event_count`, `total_message_count`, and `total_e2ee_event_count` fields to the homeserver usage statistics. ([\#18260](https://github.com/element-hq/synapse/issues/18260))

### Bugfixes

- Fix `force_tracing_for_users` config when using delegated auth. ([\#18334](https://github.com/element-hq/synapse/issues/18334))
- Fix the token introspection cache logging access tokens when MAS integration is in use. ([\#18335](https://github.com/element-hq/synapse/issues/18335))
- Stop caching introspection failures when delegating auth to MAS. ([\#18339](https://github.com/element-hq/synapse/issues/18339))
- Fix `ExternalIDReuse` exception after migrating to MAS on workers with a high traffic. ([\#18342](https://github.com/element-hq/synapse/issues/18342))
- Fix minor performance regression caused by tracking of room participation. Regressed in v1.128.0. ([\#18345](https://github.com/element-hq/synapse/issues/18345))

### Updates to the Docker image

- Optimize the build of the complement-synapse image. ([\#18294](https://github.com/element-hq/synapse/issues/18294))

### Internal Changes

- Disable statement timeout during room purge. ([\#18133](https://github.com/element-hq/synapse/issues/18133))
- Add cache to storage functions used to auth requests when using delegated auth. ([\#18337](https://github.com/element-hq/synapse/issues/18337))




# Synapse 1.128.0 (2025-04-08)

No significant changes since 1.128.0rc1.




# Synapse 1.128.0rc1 (2025-04-01)

### Features

- Add an access token introspection cache to make Matrix Authentication Service integration ([MSC3861](https://github.com/matrix-org/matrix-doc/pull/3861)) more efficient. ([\#18231](https://github.com/element-hq/synapse/issues/18231))
- Add background job to clear unreferenced state groups. ([\#18254](https://github.com/element-hq/synapse/issues/18254))
- Hashes of media files are now tracked by Synapse. Media quarantines will now apply to all files with the same hash. ([\#18277](https://github.com/element-hq/synapse/issues/18277), [\#18302](https://github.com/element-hq/synapse/issues/18302), [\#18296](https://github.com/element-hq/synapse/issues/18296))

### Bugfixes

- Add index to sliding sync ([MSC4186](https://github.com/matrix-org/matrix-doc/pull/4186)) membership snapshot table, to fix a performance issue. ([\#18074](https://github.com/element-hq/synapse/issues/18074))

### Updates to the Docker image

- Specify the architecture of installed packages via an APT config option, which is more reliable than appending package names with `:{arch}`. ([\#18271](https://github.com/element-hq/synapse/issues/18271))
- Always specify base image debian versions with a build argument. ([\#18272](https://github.com/element-hq/synapse/issues/18272))
- Allow passing arguments to `start_for_complement.sh` (to be sent to `configure_workers_and_start.py`). ([\#18273](https://github.com/element-hq/synapse/issues/18273))
- Make some improvements to the `prefix-log` script in the workers image. ([\#18274](https://github.com/element-hq/synapse/issues/18274))
- Use `uv pip` to install `supervisor` in the worker image. ([\#18275](https://github.com/element-hq/synapse/issues/18275))
- Avoid needing to download & use `rsync` in a build layer. ([\#18287](https://github.com/element-hq/synapse/issues/18287))

### Improved Documentation

- Fix how to obtain access token and change naming from riot to element ([\#18225](https://github.com/element-hq/synapse/issues/18225))
- Correct a small typo in the SSO mapping providers documentation. ([\#18276](https://github.com/element-hq/synapse/issues/18276))
- Add docs for how to clear out the Poetry wheel cache. ([\#18283](https://github.com/element-hq/synapse/issues/18283))

### Internal Changes

- Add a column `participant` to `room_memberships` table. ([\#18068](https://github.com/element-hq/synapse/issues/18068))
- Update Poetry to 2.1.1, including updating the lock file version. ([\#18251](https://github.com/element-hq/synapse/issues/18251))
- Pin GitHub Actions dependencies by commit hash. ([\#18255](https://github.com/element-hq/synapse/issues/18255))
- Add DB delta to remove the old state group deletion job. ([\#18284](https://github.com/element-hq/synapse/issues/18284))



### Updates to locked dependencies

* Bump actions/add-to-project from f5473ace9aeee8b97717b281e26980aa5097023f to 280af8ae1f83a494cfad2cb10f02f6d13529caa9. ([\#18303](https://github.com/element-hq/synapse/issues/18303))
* Bump actions/cache from 4.2.2 to 4.2.3. ([\#18266](https://github.com/element-hq/synapse/issues/18266))
* Bump actions/download-artifact from 4.2.0 to 4.2.1. ([\#18268](https://github.com/element-hq/synapse/issues/18268))
* Bump actions/setup-python from 5.4.0 to 5.5.0. ([\#18298](https://github.com/element-hq/synapse/issues/18298))
* Bump actions/upload-artifact from 4.6.1 to 4.6.2. ([\#18304](https://github.com/element-hq/synapse/issues/18304))
* Bump authlib from 1.4.1 to 1.5.1. ([\#18306](https://github.com/element-hq/synapse/issues/18306))
* Bump dawidd6/action-download-artifact from 8 to 9. ([\#18204](https://github.com/element-hq/synapse/issues/18204))
* Bump jinja2 from 3.1.5 to 3.1.6. ([\#18223](https://github.com/element-hq/synapse/issues/18223))
* Bump log from 0.4.26 to 0.4.27. ([\#18267](https://github.com/element-hq/synapse/issues/18267))
* Bump phonenumbers from 8.13.50 to 9.0.2. ([\#18299](https://github.com/element-hq/synapse/issues/18299))
* Bump pygithub from 2.5.0 to 2.6.1. ([\#18243](https://github.com/element-hq/synapse/issues/18243))
* Bump pyo3-log from 0.12.1 to 0.12.2. ([\#18269](https://github.com/element-hq/synapse/issues/18269))

# Synapse 1.127.1 (2025-03-26)

## Security
- Fix [CVE-2025-30355](https://www.cve.org/CVERecord?id=CVE-2025-30355) / [GHSA-v56r-hwv5-mxg6](https://github.com/element-hq/synapse/security/advisories/GHSA-v56r-hwv5-mxg6). **High severity vulnerability affecting federation. The vulnerability has been exploited in the wild.**



# Synapse 1.127.0 (2025-03-25)

No significant changes since 1.127.0rc1.




# Synapse 1.127.0rc1 (2025-03-18)

### Features

- Update [MSC4140](https://github.com/matrix-org/matrix-spec-proposals/pull/4140) implementation to no longer cancel a user's own delayed state events with an event type & state key that match a more recent state event sent by that user. ([\#17810](https://github.com/element-hq/synapse/issues/17810))

### Improved Documentation

- Fixed a minor typo in the Synapse documentation. Contributed by @karuto12. ([\#18224](https://github.com/element-hq/synapse/issues/18224))

### Internal Changes

- Remove undocumented `SYNAPSE_USE_FROZEN_DICTS` environment variable. ([\#18123](https://github.com/element-hq/synapse/issues/18123))
- Fix detection of workflow failures in the release script. ([\#18211](https://github.com/element-hq/synapse/issues/18211))
- Add caching support to media endpoints. ([\#18235](https://github.com/element-hq/synapse/issues/18235))



### Updates to locked dependencies

* Bump anyhow from 1.0.96 to 1.0.97. ([\#18201](https://github.com/element-hq/synapse/issues/18201))
* Bump bcrypt from 4.2.1 to 4.3.0. ([\#18207](https://github.com/element-hq/synapse/issues/18207))
* Bump bytes from 1.10.0 to 1.10.1. ([\#18227](https://github.com/element-hq/synapse/issues/18227))
* Bump http from 1.2.0 to 1.3.1. ([\#18245](https://github.com/element-hq/synapse/issues/18245))
* Bump sentry-sdk from 2.19.2 to 2.22.0. ([\#18205](https://github.com/element-hq/synapse/issues/18205))
* Bump serde from 1.0.218 to 1.0.219. ([\#18228](https://github.com/element-hq/synapse/issues/18228))
* Bump serde_json from 1.0.139 to 1.0.140. ([\#18202](https://github.com/element-hq/synapse/issues/18202))
* Bump ulid from 1.2.0 to 1.2.1. ([\#18246](https://github.com/element-hq/synapse/issues/18246))

# Synapse 1.126.0 (2025-03-11)
Administrators using the Debian/Ubuntu packages from `packages.matrix.org`, please check
[the relevant section in the upgrade notes](https://github.com/element-hq/synapse/blob/release-v1.126/docs/upgrade.md#change-of-signing-key-expiry-date-for-the-debianubuntu-package-repository)
as we have recently updated the expiry date on the repository's GPG signing key. The old version of the key will expire on `2025-03-15`.

No significant changes since 1.126.0rc3.




# Synapse 1.126.0rc3 (2025-03-07)

### Bugfixes

- Revert the background job to clear unreferenced state groups (that was introduced in v1.126.0rc1), due to [a suspected issue](https://github.com/element-hq/synapse/issues/18217) that causes increased disk usage. ([\#18222](https://github.com/element-hq/synapse/issues/18222))




# Synapse 1.126.0rc2 (2025-03-05)


### Internal Changes

- Fix wheel building configuration in CI by installing libatomic1. ([\#18212](https://github.com/element-hq/synapse/issues/18212), [\#18213](https://github.com/element-hq/synapse/issues/18213))

# Synapse 1.126.0rc1 (2025-03-04)

Synapse 1.126.0rc1 was not fully released due to an error in CI.

### Features

- Define ratelimit configuration for delayed event management. ([\#18019](https://github.com/element-hq/synapse/issues/18019))
- Add `form_secret_path` config option. ([\#18090](https://github.com/element-hq/synapse/issues/18090))
- Add the `--no-secrets-in-config` command line option. ([\#18092](https://github.com/element-hq/synapse/issues/18092))
- Add background job to clear unreferenced state groups. ([\#18154](https://github.com/element-hq/synapse/issues/18154))
- Add support for specifying/overriding `id_token_signing_alg_values_supported` for an OpenID identity provider. ([\#18177](https://github.com/element-hq/synapse/issues/18177))
- Add `worker_replication_secret_path` config option. ([\#18191](https://github.com/element-hq/synapse/issues/18191))
- Add support for specifying/overriding `redirect_uri` in the authorization and token requests against an OpenID identity provider. ([\#18197](https://github.com/element-hq/synapse/issues/18197))

### Bugfixes

- Make sure we advertise registration as disabled when [MSC3861](https://github.com/matrix-org/matrix-spec-proposals/pull/3861) is enabled. ([\#17661](https://github.com/element-hq/synapse/issues/17661))
- Prevent suspended users from sending encrypted messages. ([\#18157](https://github.com/element-hq/synapse/issues/18157))
- Cleanup deleted state group references. ([\#18165](https://github.com/element-hq/synapse/issues/18165))
- Fix [MSC4108 QR-code login](https://github.com/matrix-org/matrix-spec-proposals/pull/4108) not working with some reverse-proxy setups. ([\#18178](https://github.com/element-hq/synapse/issues/18178))
- Support device IDs that can't be represented in a scope when delegating auth to Matrix Authentication Service 0.15.0+. ([\#18174](https://github.com/element-hq/synapse/issues/18174))

### Updates to the Docker image

- Speed up the building of the Docker image. ([\#18038](https://github.com/element-hq/synapse/issues/18038))

### Improved Documentation

- Move incorrectly placed version indicator in User Event Redaction Admin API docs. ([\#18152](https://github.com/element-hq/synapse/issues/18152))
- Document suspension Admin API. ([\#18162](https://github.com/element-hq/synapse/issues/18162))

### Deprecations and Removals

- Disable room list publication by default. ([\#18175](https://github.com/element-hq/synapse/issues/18175))

### Updates to locked dependencies

* Bump anyhow from 1.0.95 to 1.0.96. ([\#18187](https://github.com/element-hq/synapse/issues/18187))
* Bump authlib from 1.4.0 to 1.4.1. ([\#18190](https://github.com/element-hq/synapse/issues/18190))
* Bump click from 8.1.7 to 8.1.8. ([\#18189](https://github.com/element-hq/synapse/issues/18189))
* Bump log from 0.4.25 to 0.4.26. ([\#18184](https://github.com/element-hq/synapse/issues/18184))
* Bump pyo3-log from 0.12.0 to 0.12.1. ([\#18046](https://github.com/element-hq/synapse/issues/18046))
* Bump serde from 1.0.217 to 1.0.218. ([\#18183](https://github.com/element-hq/synapse/issues/18183))
* Bump serde_json from 1.0.138 to 1.0.139. ([\#18186](https://github.com/element-hq/synapse/issues/18186))
* Bump sigstore/cosign-installer from 3.8.0 to 3.8.1. ([\#18185](https://github.com/element-hq/synapse/issues/18185))
* Bump types-psycopg2 from 2.9.21.20241019 to 2.9.21.20250121. ([\#18188](https://github.com/element-hq/synapse/issues/18188))


# Synapse 1.125.0 (2025-02-25)

No significant changes since 1.125.0rc1.


# Synapse 1.125.0rc1 (2025-02-18)

### Features

- Add functionality to be able to use multiple values in SSO feature `attribute_requirements`. ([\#17949](https://github.com/element-hq/synapse/issues/17949))
- Add experimental config options `admin_token_path` and `client_secret_path` for [MSC3861](https://github.com/matrix-org/matrix-spec-proposals/pull/3861). ([\#18004](https://github.com/element-hq/synapse/issues/18004))
- Add `get_current_time_msec()` method to the [module API](https://matrix-org.github.io/synapse/latest/modules/writing_a_module.html) for sound time comparisons with Synapse. ([\#18144](https://github.com/element-hq/synapse/issues/18144))

### Bugfixes

- Update the response when a client attempts to add an invalid email address to the user's account from a 500, to a 400 with error text. ([\#18125](https://github.com/element-hq/synapse/issues/18125))
- Fix user directory search when using a legacy module with a `check_username_for_spam` callback. Broke in v1.122.0. ([\#18135](https://github.com/element-hq/synapse/issues/18135))

### Updates to the Docker image

- Add `SYNAPSE_HTTP_PROXY`/`SYNAPSE_HTTPS_PROXY`/`SYNAPSE_NO_PROXY` environment variables to pass through specifically to the Synapse process (instead of needing to apply [`http_proxy`/`https_proxy`/`no_proxy`](https://element-hq.github.io/synapse/latest/setup/forward_proxy.html) globally). ([\#18158](https://github.com/element-hq/synapse/issues/18158))

### Improved Documentation

- Add Oracle Linux 8 and 9 installation instructions. ([\#17436](https://github.com/element-hq/synapse/issues/17436))
- Document missing server config options (`daemonize`, `print_pidfile`, `user_agent_suffix`, `use_frozen_dicts`, `manhole`). ([\#18122](https://github.com/element-hq/synapse/issues/18122))
- Document consequences of replacing secrets. ([\#18138](https://github.com/element-hq/synapse/issues/18138))
- Make `burst_count` field an integer in `rc_presence` config documentation example. ([\#18159](https://github.com/element-hq/synapse/issues/18159))

### Internal Changes

- Overload `DatabasePool.simple_select_one_txn` to return non-`None` when the `allow_none` parameter is `False`. ([\#17616](https://github.com/element-hq/synapse/issues/17616))
- Python 3.8 EOL: compile native extensions with the 3.9 ABI and use typing hints from the standard library. ([\#17967](https://github.com/element-hq/synapse/issues/17967))
- Add log message when worker lock timeouts get large. ([\#18124](https://github.com/element-hq/synapse/issues/18124))
- Make it explicit that you can buy an AGPL-alternative commercial license from Element. ([\#18134](https://github.com/element-hq/synapse/issues/18134))
- Fix the 'Fix linting' GitHub Actions workflow. ([\#18136](https://github.com/element-hq/synapse/issues/18136))
- Do not log at the exception-level when clients provide empty `since` token to `/sync` API. ([\#18139](https://github.com/element-hq/synapse/issues/18139))
- Reduce database load of user search when using large search terms. ([\#18172](https://github.com/element-hq/synapse/issues/18172))



### Updates to locked dependencies

* Bump bcrypt from 4.2.0 to 4.2.1. ([\#18127](https://github.com/element-hq/synapse/issues/18127))
* Bump bytes from 1.9.0 to 1.10.0. ([\#18149](https://github.com/element-hq/synapse/issues/18149))
* Bump gitpython from 3.1.43 to 3.1.44. ([\#18128](https://github.com/element-hq/synapse/issues/18128))
* Bump hiredis from 3.0.0 to 3.1.0. ([\#18169](https://github.com/element-hq/synapse/issues/18169))
* Bump serde_json from 1.0.137 to 1.0.138. ([\#18129](https://github.com/element-hq/synapse/issues/18129))
* Bump service-identity from 24.1.0 to 24.2.0. ([\#18171](https://github.com/element-hq/synapse/issues/18171))
* Bump sigstore/cosign-installer from 3.7.0 to 3.8.0. ([\#18147](https://github.com/element-hq/synapse/issues/18147))
* Bump twine from 6.0.1 to 6.1.0. ([\#18170](https://github.com/element-hq/synapse/issues/18170))
* Bump types-pyyaml from 6.0.12.20240917 to 6.0.12.20241230. ([\#18097](https://github.com/element-hq/synapse/issues/18097))
* Bump ulid from 1.1.4 to 1.2.0. ([\#18148](https://github.com/element-hq/synapse/issues/18148))

# Synapse 1.124.0 (2025-02-11)

No significant changes since 1.124.0rc3.




# Synapse 1.124.0rc3 (2025-02-07)

### Bugfixes

- Fix regression in performance of sending events due to superfluous reads and locks. Introduced in v1.124.0rc1. ([\#18141](https://github.com/element-hq/synapse/issues/18141))




# Synapse 1.124.0rc2 (2025-02-05)

### Bugfixes

- Fix regression where persisting events in some rooms could fail after a previous unclean shutdown. Introduced in v1.124.0rc1. ([\#18137](https://github.com/element-hq/synapse/issues/18137))




# Synapse 1.124.0rc1 (2025-02-04)

### Bugfixes

- Add rate limit `rc_presence.per_user`. This prevents load from excessive presence updates sent by clients via sync api. Also rate limit `/_matrix/client/v3/presence` as per the spec. Contributed by @rda0. ([\#18000](https://github.com/element-hq/synapse/issues/18000))
- Deactivated users will no longer automatically accept an invite when `auto_accept_invites` is enabled. ([\#18073](https://github.com/element-hq/synapse/issues/18073))
- Fix join being denied after being invited over federation. Also fixes other out-of-band membership transitions. ([\#18075](https://github.com/element-hq/synapse/issues/18075))
- Updates contributed `docker-compose.yml` file to PostgreSQL v15, as v12 is no longer supported by Synapse.
  Contributed by @maxkratz. ([\#18089](https://github.com/element-hq/synapse/issues/18089))
- Fix rare edge case where state groups could be deleted while we are persisting new events that reference them. ([\#18107](https://github.com/element-hq/synapse/issues/18107), [\#18130](https://github.com/element-hq/synapse/issues/18130), [\#18131](https://github.com/element-hq/synapse/issues/18131))
- Raise an error if someone is using an incorrect suffix in a config duration string. ([\#18112](https://github.com/element-hq/synapse/issues/18112))
- Fix a bug where the [Delete Room Admin API](https://element-hq.github.io/synapse/latest/admin_api/rooms.html#version-2-new-version) would fail if the `block` parameter was set to `true` and a worker other than the main process was configured to handle background tasks. ([\#18119](https://github.com/element-hq/synapse/issues/18119))

### Internal Changes

- Increase the length of the generated `nonce` parameter when perfoming OIDC logins to comply with the TI-Messenger spec. ([\#18109](https://github.com/element-hq/synapse/issues/18109))



### Updates to locked dependencies

* Bump dawidd6/action-download-artifact from 7 to 8. ([\#18108](https://github.com/element-hq/synapse/issues/18108))
* Bump log from 0.4.22 to 0.4.25. ([\#18098](https://github.com/element-hq/synapse/issues/18098))
* Bump python-multipart from 0.0.18 to 0.0.20. ([\#18096](https://github.com/element-hq/synapse/issues/18096))
* Bump serde_json from 1.0.135 to 1.0.137. ([\#18099](https://github.com/element-hq/synapse/issues/18099))
* Bump types-bleach from 6.1.0.20240331 to 6.2.0.20241123. ([\#18082](https://github.com/element-hq/synapse/issues/18082))

# Synapse 1.123.0 (2025-01-28)

No significant changes since 1.123.0rc1.




# Synapse 1.123.0rc1 (2025-01-21)

### Features

- Implement [MSC4133](https://github.com/matrix-org/matrix-spec-proposals/pull/4133) for custom profile fields. Contributed by @clokep. ([\#17488](https://github.com/element-hq/synapse/issues/17488))
- Add a query parameter `type` to the [Room State Admin API](https://element-hq.github.io/synapse/develop/admin_api/rooms.html#room-state-api) that filters the state event. ([\#18035](https://github.com/element-hq/synapse/issues/18035))
- Support the new `/auth_metadata` endpoint defined in [MSC2965](https://github.com/matrix-org/matrix-spec-proposals/pull/2965). ([\#18093](https://github.com/element-hq/synapse/issues/18093))

### Bugfixes

- Fix membership caches not updating in state reset scenarios. ([\#17732](https://github.com/element-hq/synapse/issues/17732))
- Fix rare race where on upgrade to v1.122.0 a long running database upgrade could lock out new events from being received or sent. ([\#18091](https://github.com/element-hq/synapse/issues/18091))

### Improved Documentation

- Document `tls` option for a worker instance in `instance_map`. ([\#18064](https://github.com/element-hq/synapse/issues/18064))

### Deprecations and Removals

- Remove the unstable [MSC4151](https://github.com/matrix-org/matrix-spec-proposals/pull/4151) implementation. The stable support remains, per [Matrix 1.13](https://spec.matrix.org/v1.13/client-server-api/#post_matrixclientv3roomsroomidreport). ([\#18052](https://github.com/element-hq/synapse/issues/18052))

### Internal Changes

- Increase invite rate limits (`rc_invites.per_issuer`) for Complement. ([\#18072](https://github.com/element-hq/synapse/issues/18072))



### Updates to locked dependencies

* Bump jinja2 from 3.1.4 to 3.1.5. ([\#18067](https://github.com/element-hq/synapse/issues/18067))
* Bump mypy from 1.12.1 to 1.13.0. ([\#18083](https://github.com/element-hq/synapse/issues/18083))
* Bump pillow from 11.0.0 to 11.1.0. ([\#18084](https://github.com/element-hq/synapse/issues/18084))
* Bump pyo3 from 0.23.3 to 0.23.4. ([\#18079](https://github.com/element-hq/synapse/issues/18079))
* Bump pyopenssl from 24.2.1 to 24.3.0. ([\#18062](https://github.com/element-hq/synapse/issues/18062))
* Bump serde_json from 1.0.134 to 1.0.135. ([\#18081](https://github.com/element-hq/synapse/issues/18081))
* Bump ulid from 1.1.3 to 1.1.4. ([\#18080](https://github.com/element-hq/synapse/issues/18080))

# Synapse 1.122.0 (2025-01-14)

Please note that this version of Synapse drops support for PostgreSQL 11 and 12. The minimum version of PostgreSQL supported is now version 13.

No significant changes since 1.122.0rc1.


# Synapse 1.122.0rc1 (2025-01-07)

### Deprecations and Removals

- Remove support for PostgreSQL 11 and 12. Contributed by @clokep. ([\#18034](https://github.com/element-hq/synapse/issues/18034))

### Features

- Added the `email.tlsname` config option.  This allows specifying the domain name used to validate the SMTP server's TLS certificate separately from the `email.smtp_host` to connect to. ([\#17849](https://github.com/element-hq/synapse/issues/17849))
- Module developers will have access to the user ID of the requester when adding `check_username_for_spam` callbacks to `spam_checker_module_callbacks`. Contributed by Wilson@Pangea.chat. ([\#17916](https://github.com/element-hq/synapse/issues/17916))
- Add endpoints to the Admin API to fetch the number of invites the provided user has sent after a given timestamp,
  fetch the number of rooms the provided user has joined after a given timestamp, and get report IDs of event
  reports against a provided user (i.e. where the user was the sender of the reported event). ([\#17948](https://github.com/element-hq/synapse/issues/17948))
- Support stable account suspension from [MSC3823](https://github.com/matrix-org/matrix-spec-proposals/pull/3823). ([\#17964](https://github.com/element-hq/synapse/issues/17964))
- Add `macaroon_secret_key_path` config option. ([\#17983](https://github.com/element-hq/synapse/issues/17983))

### Bugfixes

- Fix bug when rejecting withdrew invite with a `third_party_rules` module, where the invite would be stuck for the client. ([\#17930](https://github.com/element-hq/synapse/issues/17930))
- Properly purge state groups tables when purging a room with the Admin API. ([\#18024](https://github.com/element-hq/synapse/issues/18024))
- Fix a bug preventing the admin redaction endpoint from working on messages from remote users. ([\#18029](https://github.com/element-hq/synapse/issues/18029), [\#18043](https://github.com/element-hq/synapse/issues/18043))

### Improved Documentation

- Update `synapse.app.generic_worker` documentation to only recommend `GET` requests for stream writer routes by default, unless the worker is also configured as a stream writer. Contributed by @evoL. ([\#17954](https://github.com/element-hq/synapse/issues/17954))
- Add documentation for the previously-undocumented `last_seen_ts` query parameter to the query user Admin API. ([\#17976](https://github.com/element-hq/synapse/issues/17976))
- Improve documentation for the `TaskScheduler` class. ([\#17992](https://github.com/element-hq/synapse/issues/17992))
- Fix example in reverse proxy docs to include server port. ([\#17994](https://github.com/element-hq/synapse/issues/17994))
- Update Alpine Linux Synapse Package Maintainer within the installation instructions. ([\#17846](https://github.com/element-hq/synapse/issues/17846))

### Internal Changes

- Add `RoomID` & `EventID` rust types. ([\#17996](https://github.com/element-hq/synapse/issues/17996))
- Fix various type errors across the codebase. ([\#17998](https://github.com/element-hq/synapse/issues/17998))
- Disable DB statement timeout when doing a room purge since it can be quite long. ([\#18017](https://github.com/element-hq/synapse/issues/18017))
- Remove some remaining uses of `twisted.internet.defer.returnValue`. Contributed by Colin Watson. ([\#18020](https://github.com/element-hq/synapse/issues/18020))
- Refactor `get_profile` to no longer include fields with a value of `None`. ([\#18063](https://github.com/element-hq/synapse/issues/18063))

### Updates to locked dependencies

* Bump anyhow from 1.0.93 to 1.0.95. ([\#18012](https://github.com/element-hq/synapse/issues/18012), [\#18045](https://github.com/element-hq/synapse/issues/18045))
* Bump authlib from 1.3.2 to 1.4.0. ([\#18048](https://github.com/element-hq/synapse/issues/18048))
* Bump dawidd6/action-download-artifact from 6 to 7. ([\#17981](https://github.com/element-hq/synapse/issues/17981))
* Bump http from 1.1.0 to 1.2.0. ([\#18013](https://github.com/element-hq/synapse/issues/18013))
- Bump mypy from 1.11.2 to 1.12.1. ([\#17999](https://github.com/element-hq/synapse/issues/17999))
* Bump mypy-zope from 1.0.8 to 1.0.9. ([\#18047](https://github.com/element-hq/synapse/issues/18047))
* Bump pillow from 10.4.0 to 11.0.0. ([\#18015](https://github.com/element-hq/synapse/issues/18015))
* Bump pydantic from 2.9.2 to 2.10.3. ([\#18014](https://github.com/element-hq/synapse/issues/18014))
* Bump pyicu from 2.13.1 to 2.14. ([\#18060](https://github.com/element-hq/synapse/issues/18060))
* Bump pyo3 from 0.23.2 to 0.23.3. ([\#18001](https://github.com/element-hq/synapse/issues/18001))
* Bump python-multipart from 0.0.16 to 0.0.18. ([\#17985](https://github.com/element-hq/synapse/issues/17985))
* Bump sentry-sdk from 2.17.0 to 2.19.2. ([\#18061](https://github.com/element-hq/synapse/issues/18061))
* Bump serde from 1.0.215 to 1.0.217. ([\#18031](https://github.com/element-hq/synapse/issues/18031), [\#18059](https://github.com/element-hq/synapse/issues/18059))
* Bump serde_json from 1.0.133 to 1.0.134. ([\#18044](https://github.com/element-hq/synapse/issues/18044))
* Bump twine from 5.1.1 to 6.0.1. ([\#18049](https://github.com/element-hq/synapse/issues/18049))

**Changelogs for older versions can be found [here](docs/changelogs/).**
