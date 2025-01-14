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
