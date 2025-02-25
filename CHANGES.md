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
