# Synapse 1.121.1 (2024-12-11)

This release contains a fix for our docker build CI. It is functionally identical to 1.121.0, whose changelog is below.

### Internal Changes

- Downgrade the Ubuntu GHA runner when building docker images. ([\#18026](https://github.com/element-hq/synapse/issues/18026))



# Synapse 1.121.0 (2024-12-11)

### Internal Changes

- Fix release process to not create duplicate releases. ([\#18025](https://github.com/element-hq/synapse/issues/18025))



# Synapse 1.121.0rc1 (2024-12-04)

### Features

- Support for [MSC4190](https://github.com/matrix-org/matrix-spec-proposals/pull/4190): device management for Application Services. ([\#17705](https://github.com/element-hq/synapse/issues/17705))
- Update [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) Sliding Sync to include invite, ban, kick, targets when `$LAZY`-loading room members. ([\#17947](https://github.com/element-hq/synapse/issues/17947))
- Use stable `M_USER_LOCKED` error code for locked accounts, as per [Matrix 1.12](https://spec.matrix.org/v1.12/client-server-api/#account-locking). ([\#17965](https://github.com/element-hq/synapse/issues/17965))
- [MSC4076](https://github.com/matrix-org/matrix-spec-proposals/pull/4076): Add `disable_badge_count` to pusher configuration. ([\#17975](https://github.com/element-hq/synapse/issues/17975))

### Bugfixes

- Fix long-standing bug where read receipts could get overly delayed being sent over federation. ([\#17933](https://github.com/element-hq/synapse/issues/17933))

### Improved Documentation

- Add OIDC example configuration for Forgejo (fork of Gitea). ([\#17872](https://github.com/element-hq/synapse/issues/17872))
- Link to element-docker-demo from contrib/docker*. ([\#17953](https://github.com/element-hq/synapse/issues/17953))

### Internal Changes

- [MSC4108](https://github.com/matrix-org/matrix-spec-proposals/pull/4108): Add a `Content-Type` header on the `PUT` response to work around a faulty behavior in some caching reverse proxies. ([\#17253](https://github.com/element-hq/synapse/issues/17253))
- Fix incorrect comment in new schema delta. ([\#17936](https://github.com/element-hq/synapse/issues/17936))
- Raise setuptools_rust version cap to 1.10.2. ([\#17944](https://github.com/element-hq/synapse/issues/17944))
- Enable encrypted appservice related experimental features in the complement docker image. ([\#17945](https://github.com/element-hq/synapse/issues/17945))
- Return whether the user is suspended when querying the user account in the Admin API. ([\#17952](https://github.com/element-hq/synapse/issues/17952))
- Fix new scheduled tasks jumping the queue. ([\#17962](https://github.com/element-hq/synapse/issues/17962))
- Bump pyo3 and dependencies to v0.23.2. ([\#17966](https://github.com/element-hq/synapse/issues/17966))
- Update setuptools-rust and fix building abi3 wheels in latest version. ([\#17969](https://github.com/element-hq/synapse/issues/17969))
- Consolidate SSO redirects through `/_matrix/client/v3/login/sso/redirect(/{idpId})`. ([\#17972](https://github.com/element-hq/synapse/issues/17972))
- Fix Docker and Complement config to be able to use `public_baseurl`. ([\#17986](https://github.com/element-hq/synapse/issues/17986))
- Fix building wheels for MacOS which was temporarily disabled in Synapse 1.120.2. ([\#17993](https://github.com/element-hq/synapse/issues/17993))
- Fix release process to not create duplicate releases. ([\#17970](https://github.com/element-hq/synapse/issues/17970), [\#17995](https://github.com/element-hq/synapse/issues/17995))


### Updates to locked dependencies

* Bump bytes from 1.8.0 to 1.9.0. ([\#17982](https://github.com/element-hq/synapse/issues/17982))
* Bump pysaml2 from 7.3.1 to 7.5.0. ([\#17978](https://github.com/element-hq/synapse/issues/17978))
* Bump serde_json from 1.0.132 to 1.0.133. ([\#17939](https://github.com/element-hq/synapse/issues/17939))
* Bump tomli from 2.0.2 to 2.1.0. ([\#17959](https://github.com/element-hq/synapse/issues/17959))
* Bump tomli from 2.1.0 to 2.2.1. ([\#17979](https://github.com/element-hq/synapse/issues/17979))
* Bump tornado from 6.4.1 to 6.4.2. ([\#17955](https://github.com/element-hq/synapse/issues/17955))

# Synapse 1.120.2 (2024-12-03)

This version has building of wheels for macOS disabled.
It is functionally identical to 1.120.1, which contains multiple security fixes.
If you are already using 1.120.1, there is no need to upgrade to this version.



# Synapse 1.120.1 (2024-12-03)

This patch release fixes multiple security vulnerabilities, some affecting all prior versions of Synapse. Server administrators are encouraged to update Synapse as soon as possible. We are not aware of these vulnerabilities being exploited in the wild.

Administrators who are unable to update Synapse may use the workarounds described in the linked GitHub Security Advisory below.

### Security advisory

The following issues are fixed in 1.120.1.

- [GHSA-rfq8-j7rh-8hf2](https://github.com/element-hq/synapse/security/advisories/GHSA-rfq8-j7rh-8hf2) / [CVE-2024-52805](https://cve.mitre.org/cgi-bin/cvename.cgi?name=CVE-2024-52805): **Unsupported content types can lead to memory exhaustion**

  Synapse instances which have a high `max_upload_size` and which don't have a reverse proxy in front of them that would otherwise limit upload size are affected.

  Fixed by [4b7154c58501b4bf5e1c2d6c11ebef96529f2fdf](https://github.com/element-hq/synapse/commit/4b7154c58501b4bf5e1c2d6c11ebef96529f2fdf).

- [GHSA-f3r3-h2mq-hx2h](https://github.com/element-hq/synapse/security/advisories/GHSA-f3r3-h2mq-hx2h) / [CVE-2024-52815](https://cve.mitre.org/cgi-bin/cvename.cgi?name=CVE-2024-52815): **Malicious invites via federation can break a user's sync**

  Fixed by [d82e1ed357b7ee21dff83d06cba7a67840cfd464](https://github.com/element-hq/synapse/commit/d82e1ed357b7ee21dff83d06cba7a67840cfd464).

- [GHSA-vp6v-whfm-rv3g](https://github.com/element-hq/synapse/security/advisories/GHSA-vp6v-whfm-rv3g) / [CVE-2024-53863](https://cve.mitre.org/cgi-bin/cvename.cgi?name=CVE-2024-53863): **Synapse can be forced to thumbnail unexpected file formats, invoking potentially untrustworthy decoders**

  Synapse instances can disable dynamic thumbnailing by setting `dynamic_thumbnails` to `false` in the configuration file.

  Fixed by [b64a4e5fbbbf119b6c65aedf0d999b4237d55503](https://github.com/element-hq/synapse/commit/b64a4e5fbbbf119b6c65aedf0d999b4237d55503).

- [GHSA-56w4-5538-8v8h](https://github.com/element-hq/synapse/security/advisories/GHSA-56w4-5538-8v8h) / [CVE-2024-53867](https://cve.mitre.org/cgi-bin/cvename.cgi?name=CVE-2024-53867): **The Sliding Sync feature on Synapse versions between 1.113.0rc1 and 1.120.0 can leak partial room state changes to users no longer in a room**

  Non-state events, like messages, are unaffected.

  Synapse instances can disable the Sliding Sync feature by setting `experimental_features.msc3575_enabled` to `false` in the configuration file.

  Fixed by [4daa533e82f345ce87b9495d31781af570ba3ead](https://github.com/element-hq/synapse/commit/4daa533e82f345ce87b9495d31781af570ba3ead).

See the advisories for more details. If you have any questions, email [security at element.io](mailto:security@element.io).

### Bugfixes

- Fix release process to not create duplicate releases. ([\#17970](https://github.com/element-hq/synapse/issues/17970))



# Synapse 1.120.0 (2024-11-26)

### Bugfixes

- Fix a bug introduced in Synapse v1.120rc1 which would cause the newly-introduced `delete_old_otks` job to fail in worker-mode deployments. ([\#17960](https://github.com/element-hq/synapse/issues/17960))




# Synapse 1.120.0rc1 (2024-11-20)

This release enables the enforcement of authenticated media by default, with exemptions for media that is already present in the
homeserver's media store.

Most homeservers operating in the public federation will not be impacted by this change, given that
the large homeserver `matrix.org` enabled this in September 2024 and therefore most clients and servers
will already have updated as a result.

Some server administrators may still wish to disable this enforcement for the time being, in the interest of compatibility with older clients
and older federated homeservers.
See the [upgrade notes](https://element-hq.github.io/synapse/v1.120/upgrade.html#authenticated-media-is-now-enforced-by-default) for more information.

### Features

- Enforce authenticated media by default. Administrators can revert this by configuring `enable_authenticated_media` to `false`. In a future release of Synapse, this option will be removed and become always-on. ([\#17889](https://github.com/element-hq/synapse/issues/17889))
- Add a one-off task to delete old One-Time Keys, to guard against us having old OTKs in the database that the client has long forgotten about. ([\#17934](https://github.com/element-hq/synapse/issues/17934))

### Improved Documentation

- Clarify the semantics of the `enable_authenticated_media` configuration option. ([\#17913](https://github.com/element-hq/synapse/issues/17913))
- Add documentation about backing up Synapse. ([\#17931](https://github.com/element-hq/synapse/issues/17931))

### Deprecations and Removals

- Remove support for [MSC3886: Simple client rendezvous capability](https://github.com/matrix-org/matrix-spec-proposals/pull/3886), which has been superseded by [MSC4108](https://github.com/matrix-org/matrix-spec-proposals/pull/4108) and therefore closed. ([\#17638](https://github.com/element-hq/synapse/issues/17638))

### Internal Changes

- Addressed some typos in docs and returned error message for unknown MXC ID. ([\#17865](https://github.com/element-hq/synapse/issues/17865))
- Unpin the upload release GHA action. ([\#17923](https://github.com/element-hq/synapse/issues/17923))
- Bump macOS version used to build wheels during release, as current version used is end-of-life. ([\#17924](https://github.com/element-hq/synapse/issues/17924))
- Move server event filtering logic to Rust. ([\#17928](https://github.com/element-hq/synapse/issues/17928))
- Support new package name of PyPI package `python-multipart` 0.0.13 so that distro packagers do not need to work around name conflict with PyPI package `multipart`. ([\#17932](https://github.com/element-hq/synapse/issues/17932))
- Speed up slow initial sliding syncs on large servers. ([\#17946](https://github.com/element-hq/synapse/issues/17946))

### Updates to locked dependencies

* Bump anyhow from 1.0.92 to 1.0.93. ([\#17920](https://github.com/element-hq/synapse/issues/17920))
* Bump bleach from 6.1.0 to 6.2.0. ([\#17918](https://github.com/element-hq/synapse/issues/17918))
* Bump immutabledict from 4.2.0 to 4.2.1. ([\#17941](https://github.com/element-hq/synapse/issues/17941))
* Bump packaging from 24.1 to 24.2. ([\#17940](https://github.com/element-hq/synapse/issues/17940))
* Bump phonenumbers from 8.13.49 to 8.13.50. ([\#17942](https://github.com/element-hq/synapse/issues/17942))
* Bump pygithub from 2.4.0 to 2.5.0. ([\#17917](https://github.com/element-hq/synapse/issues/17917))
* Bump ruff from 0.7.2 to 0.7.3. ([\#17919](https://github.com/element-hq/synapse/issues/17919))
* Bump serde from 1.0.214 to 1.0.215. ([\#17938](https://github.com/element-hq/synapse/issues/17938))

# Synapse 1.119.0 (2024-11-13)

No significant changes since 1.119.0rc2.

### Python 3.8 support dropped

Python 3.8 is [end-of-life](https://devguide.python.org/versions/) and is no longer supported by Synapse. The minimum supported Python version is now 3.9.

If you are running Synapse with Python 3.8, please upgrade to Python 3.9 (or greater) before upgrading Synapse.


# Synapse 1.119.0rc2 (2024-11-11)

Note that due to packaging issues there was no v1.119.0rc1.


### Features

- Support [MSC4151](https://github.com/matrix-org/matrix-spec-proposals/pull/4151)'s stable report room API. ([\#17374](https://github.com/element-hq/synapse/issues/17374))
- Add experimental support for [MSC4222](https://github.com/matrix-org/matrix-spec-proposals/pull/4222) (Adding `state_after` to sync v2). ([\#17888](https://github.com/element-hq/synapse/issues/17888))

### Bugfixes

- Fix bug with sliding sync where `$LAZY`-loading room members would not return `required_state` membership in incremental syncs. ([\#17809](https://github.com/element-hq/synapse/issues/17809))
- Check if user has membership in a room before tagging it. Contributed by Lama Alosaimi. ([\#17839](https://github.com/element-hq/synapse/issues/17839))
- Fix a bug in the admin redact endpoint where the background task would not run if a worker was specified in
  the config option `run_background_tasks_on`. ([\#17847](https://github.com/element-hq/synapse/issues/17847))
- Fix bug where some presence and typing timeouts can expire early. ([\#17850](https://github.com/element-hq/synapse/issues/17850))
- Fix detection when the built Rust library was outdated when using source installations. ([\#17861](https://github.com/element-hq/synapse/issues/17861))
- Fix a long-standing bug in Synapse which could cause one-time keys to be issued in the incorrect order, causing message decryption failures. ([\#17903](https://github.com/element-hq/synapse/pull/17903))
- Fix experimental support for [MSC4222](https://github.com/matrix-org/matrix-spec-proposals/pull/4222) (Adding `state_after` to sync v2) where we would return the full state on incremental syncs when using lazy loaded members and there were no new events in the timeline. ([\#17915](https://github.com/element-hq/synapse/pull/17915))

### Internal Changes

- Remove support for python 3.8. ([\#17908](https://github.com/element-hq/synapse/issues/17908))
- Add a test for downloading and thumbnailing a CMYK JPEG. ([\#17786](https://github.com/element-hq/synapse/issues/17786))
- Refactor database calls to remove `Generator` usage. ([\#17813](https://github.com/element-hq/synapse/issues/17813), [\#17814](https://github.com/element-hq/synapse/issues/17814), [\#17815](https://github.com/element-hq/synapse/issues/17815), [\#17816](https://github.com/element-hq/synapse/issues/17816), [\#17817](https://github.com/element-hq/synapse/issues/17817), [\#17818](https://github.com/element-hq/synapse/issues/17818), [\#17890](https://github.com/element-hq/synapse/issues/17890))
- Include the destination in the error of 'Destination mismatch' on federation requests. ([\#17830](https://github.com/element-hq/synapse/issues/17830))
- The nix flake inside the repository no longer tracks nixpkgs/master to not catch the latest bugs from a PR merged 5 minutes ago. ([\#17852](https://github.com/element-hq/synapse/issues/17852))
- Minor speed-up of sliding sync by computing extensions results in parallel. ([\#17884](https://github.com/element-hq/synapse/issues/17884))
- Bump the default Python version in the Synapse Dockerfile from 3.11 -> 3.12. ([\#17887](https://github.com/element-hq/synapse/issues/17887))
- Remove usage of internal header encoding API. ([\#17894](https://github.com/element-hq/synapse/issues/17894))
- Use unique name for each os.arch variant when uploading Wheel artifacts. ([\#17905](https://github.com/element-hq/synapse/issues/17905))
- Fix tests to run with latest Twisted. ([\#17906](https://github.com/element-hq/synapse/pull/17906), [\#17907](https://github.com/element-hq/synapse/pull/17907), [\#17911](https://github.com/element-hq/synapse/pull/17911))
- Update version constraint to allow the latest poetry-core 1.9.1. ([\#17902](https://github.com/element-hq/synapse/pull/17902))
- Update the portdb CI to use Python 3.13 and Postgres 17 as latest dependencies. ([\#17909](https://github.com/element-hq/synapse/pull/17909))
- Add an index to `current_state_delta_stream` table. ([\#17912](https://github.com/element-hq/synapse/issues/17912))
- Fix building and attaching release artifacts during the release process. ([\#17921](https://github.com/element-hq/synapse/issues/17921))

### Updates to locked dependencies

* Bump actions/download-artifact & actions/upload-artifact from 3 to 4 in /.github/workflows. ([\#17657](https://github.com/element-hq/synapse/issues/17657))
* Bump anyhow from 1.0.89 to 1.0.92. ([\#17858](https://github.com/element-hq/synapse/issues/17858), [\#17876](https://github.com/element-hq/synapse/issues/17876), [\#17901](https://github.com/element-hq/synapse/issues/17901))
* Bump bytes from 1.7.2 to 1.8.0. ([\#17877](https://github.com/element-hq/synapse/issues/17877))
* Bump cryptography from 43.0.1 to 43.0.3. ([\#17853](https://github.com/element-hq/synapse/issues/17853))
* Bump mypy-zope from 1.0.7 to 1.0.8. ([\#17898](https://github.com/element-hq/synapse/issues/17898))
* Bump phonenumbers from 8.13.47 to 8.13.49. ([\#17880](https://github.com/element-hq/synapse/issues/17880), [\#17899](https://github.com/element-hq/synapse/issues/17899))
* Bump python-multipart from 0.0.12 to 0.0.16. ([\#17879](https://github.com/element-hq/synapse/issues/17879))
* Bump regex from 1.11.0 to 1.11.1. ([\#17874](https://github.com/element-hq/synapse/issues/17874))
* Bump ruff from 0.6.9 to 0.7.2. ([\#17868](https://github.com/element-hq/synapse/issues/17868), [\#17897](https://github.com/element-hq/synapse/issues/17897))
* Bump serde from 1.0.210 to 1.0.214. ([\#17875](https://github.com/element-hq/synapse/issues/17875), [\#17900](https://github.com/element-hq/synapse/issues/17900))
* Bump serde_json from 1.0.128 to 1.0.132. ([\#17857](https://github.com/element-hq/synapse/issues/17857))
* Bump types-psycopg2 from 2.9.21.20240819 to 2.9.21.20241019. ([\#17855](https://github.com/element-hq/synapse/issues/17855))
* Bump types-setuptools from 75.1.0.20241014 to 75.2.0.20241019. ([\#17856](https://github.com/element-hq/synapse/issues/17856))

# Synapse 1.118.0 (2024-10-29)

No significant changes since 1.118.0rc1.

### Python 3.8 support will be dropped in the next release

Python 3.8 is now [end-of-life](https://devguide.python.org/versions/). As per our [Deprecation Policy for Platform Dependencies](https://element-hq.github.io/synapse/latest/deprecation_policy.html#policy), Synapse will be dropping support for Python 3.8 in the next release; Synapse 1.119.0.

Synapse 1.118.x will be the final release to support Python 3.8. If you are running Synapse with Python 3.8, please upgrade before the 1.119.0 release, due in less than one month.

### Python 3.13 and PostgreSQL 17 support

On the other end of the spectrum, Synapse 1.118.0 is the first release to support [Python 3.13](https://www.python.org/downloads/release/python-3130/)! [PostgreSQL 17](https://www.postgresql.org/about/news/postgresql-17-released-2936/) is also supported as of this release.


# Synapse 1.118.0rc1 (2024-10-22)

### Features

- Added the `display_name_claim` option to the JWT configuration. This option allows specifying the claim key that contains the user's display name in the JWT payload. ([\#17708](https://github.com/element-hq/synapse/issues/17708))
- Implement [MSC4210](https://github.com/matrix-org/matrix-spec-proposals/pull/4210): Remove legacy mentions. Contributed by @tulir @ Beeper. ([\#17783](https://github.com/element-hq/synapse/issues/17783))

### Bugfixes

- Fix saving of PNG thumbnails, when the original image is in the CMYK color space. ([\#17736](https://github.com/element-hq/synapse/issues/17736))
- Fix bug with sliding sync where the server would not return state that was added to the `required_state` config. ([\#17785](https://github.com/element-hq/synapse/issues/17785), [\#17805](https://github.com/element-hq/synapse/issues/17805))
- Fix a bug in [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) Sliding Sync that would cause rooms to stay forgotten and hidden even after rejoining. ([\#17835](https://github.com/element-hq/synapse/issues/17835))

### Improved Documentation

- Clarify when the `user_may_invite` and `user_may_send_3pid_invite` module callbacks are called. ([\#17627](https://github.com/element-hq/synapse/issues/17627))
- Correct documentation to refer to the `--config-path` argument instead of `--config-file`. ([\#17802](https://github.com/element-hq/synapse/issues/17802))
- Fix typo in `target_cache_memory_usage` docs. ([\#17825](https://github.com/element-hq/synapse/issues/17825))

### Internal Changes

- Slight optimization when fetching state/events for Sliding Sync. ([\#17718](https://github.com/element-hq/synapse/issues/17718))
- Add Python 3.13 and Postgres 17 to the test matrix. ([\#17752](https://github.com/element-hq/synapse/issues/17752))
- Test github token before running release script steps. ([\#17803](https://github.com/element-hq/synapse/issues/17803))
- Build debian packages for new Ubuntu versions, and stop building for no longer supported versions. ([\#17824](https://github.com/element-hq/synapse/issues/17824))
- Enable the `.org.matrix.msc4028.encrypted_event` push rule by default in accordance with [MSC4028](https://github.com/matrix-org/matrix-spec-proposals/pull/4028). Note that the corresponding experimental feature must still be switched on for this push rule to have any effect. ([\#17826](https://github.com/element-hq/synapse/issues/17826))
- Fix some typing issues uncovered by upgrading mypy to 1.11.x. ([\#17842](https://github.com/element-hq/synapse/issues/17842))



### Updates to locked dependencies

* Bump mypy from 1.10.1 to 1.11.2. ([\#17842](https://github.com/element-hq/synapse/issues/17842))
* Bump mypy-zope from 1.0.5 to 1.0.7. ([\#17827](https://github.com/element-hq/synapse/issues/17827))
* Bump phonenumbers from 8.13.46 to 8.13.47. ([\#17797](https://github.com/element-hq/synapse/issues/17797))
* Bump psycopg2 from 2.9.9 to 2.9.10. ([\#17843](https://github.com/element-hq/synapse/issues/17843))
* Bump ruff from 0.6.8 to 0.6.9. ([\#17794](https://github.com/element-hq/synapse/issues/17794))
* Bump sentry-sdk from 2.14.0 to 2.15.0. ([\#17795](https://github.com/element-hq/synapse/issues/17795))
* Bump sentry-sdk from 2.15.0 to 2.16.0. ([\#17829](https://github.com/element-hq/synapse/issues/17829))
* Bump sentry-sdk from 2.16.0 to 2.17.0. ([\#17844](https://github.com/element-hq/synapse/issues/17844))
* Bump sigstore/cosign-installer from 3.6.0 to 3.7.0. ([\#17798](https://github.com/element-hq/synapse/issues/17798))
* Bump tomli from 2.0.1 to 2.0.2. ([\#17796](https://github.com/element-hq/synapse/issues/17796))
* Bump types-requests from 2.32.0.20240914 to 2.32.0.20241016. ([\#17841](https://github.com/element-hq/synapse/issues/17841))
* Bump types-setuptools from 75.1.0.20240917 to 75.1.0.20241014. ([\#17828](https://github.com/element-hq/synapse/issues/17828))

# Synapse 1.117.0 (2024-10-15)

No significant changes since 1.117.0rc1.




# Synapse 1.117.0rc1 (2024-10-08)

### Features

- Add config option `redis.password_path`. ([\#17717](https://github.com/element-hq/synapse/issues/17717))

### Bugfixes

- Fix a rare bug introduced in v1.29.0 where invalidating a user's access token from a worker could raise an error. ([\#17779](https://github.com/element-hq/synapse/issues/17779))
- In the response to `GET /_matrix/client/versions`, set the `unstable_features` flag for [MSC4140](https://github.com/matrix-org/matrix-spec-proposals/pull/4140) to `false` when server configuration disables support for delayed events. ([\#17780](https://github.com/element-hq/synapse/issues/17780))
- Improve input validation and room membership checks in admin redaction API. ([\#17792](https://github.com/element-hq/synapse/issues/17792))

### Improved Documentation

- Clarify the docstring of `test_forget_when_not_left`. ([\#17628](https://github.com/element-hq/synapse/issues/17628))
- Add documentation note about PYTHONMALLOC for accurate jemalloc memory tracking. Contributed by @hensg. ([\#17709](https://github.com/element-hq/synapse/issues/17709))
- Remove spurious "TODO UPDATE ALL THIS" note in the Debian installation docs. ([\#17749](https://github.com/element-hq/synapse/issues/17749))
- Explain how load balancing works for `federation_sender_instances`. ([\#17776](https://github.com/element-hq/synapse/issues/17776))

### Internal Changes

- Minor performance increase for large accounts using sliding sync. ([\#17751](https://github.com/element-hq/synapse/issues/17751))
- Increase performance of the notifier when there are many syncing users. ([\#17765](https://github.com/element-hq/synapse/issues/17765), [\#17766](https://github.com/element-hq/synapse/issues/17766))
- Fix performance of streams that don't change often. ([\#17767](https://github.com/element-hq/synapse/issues/17767))
- Improve performance of sliding sync connections that do not ask for any rooms. ([\#17768](https://github.com/element-hq/synapse/issues/17768))
- Reduce overhead of sliding sync E2EE loops. ([\#17771](https://github.com/element-hq/synapse/issues/17771))
- Sliding sync minor performance speed up using new table. ([\#17787](https://github.com/element-hq/synapse/issues/17787))
- Sliding sync minor performance improvement by omitting unchanged data from incremental responses. ([\#17788](https://github.com/element-hq/synapse/issues/17788))
- Speed up sliding sync when there are many active subscriptions. ([\#17789](https://github.com/element-hq/synapse/issues/17789))
- Add missing license headers on new source files. ([\#17799](https://github.com/element-hq/synapse/issues/17799))



### Updates to locked dependencies

* Bump phonenumbers from 8.13.45 to 8.13.46. ([\#17773](https://github.com/element-hq/synapse/issues/17773))
* Bump python-multipart from 0.0.10 to 0.0.12. ([\#17772](https://github.com/element-hq/synapse/issues/17772))
* Bump regex from 1.10.6 to 1.11.0. ([\#17770](https://github.com/element-hq/synapse/issues/17770))
* Bump ruff from 0.6.7 to 0.6.8. ([\#17774](https://github.com/element-hq/synapse/issues/17774))

# Synapse 1.116.0 (2024-10-01)

No significant changes since 1.116.0rc2.




# Synapse 1.116.0rc2 (2024-09-26)

### Features

- Add implementation of restricting who can overwrite a state event as proposed by [MSC3757](https://github.com/matrix-org/matrix-spec-proposals/pull/3757). ([\#17513](https://github.com/element-hq/synapse/issues/17513))




# Synapse 1.116.0rc1 (2024-09-25)

### Features

- Add initial implementation of delayed events as proposed by [MSC4140](https://github.com/matrix-org/matrix-spec-proposals/pull/4140). ([\#17326](https://github.com/element-hq/synapse/issues/17326))
- Add an asynchronous Admin API endpoint [to redact all a user's events](https://element-hq.github.io/synapse/v1.116/admin_api/user_admin_api.html#redact-all-the-events-of-a-user),
  and [an endpoint to check on the status of that redaction task](https://element-hq.github.io/synapse/v1.116/admin_api/user_admin_api.html#check-the-status-of-a-redaction-process). ([\#17506](https://github.com/element-hq/synapse/issues/17506))
- Add support for the `tags` and `not_tags` filters for [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) Sliding Sync. ([\#17662](https://github.com/element-hq/synapse/issues/17662))
- Guests can use the new media endpoints to download media, as described by [MSC4189](https://github.com/matrix-org/matrix-spec-proposals/pull/4189). ([\#17675](https://github.com/element-hq/synapse/issues/17675))
- Add config option `turn_shared_secret_path`. ([\#17690](https://github.com/element-hq/synapse/issues/17690))
- Return room tags in [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) Sliding Sync account data extension. ([\#17707](https://github.com/element-hq/synapse/issues/17707))

### Bugfixes

- Make sure we get up-to-date state information when using the new [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) Sliding Sync tables to derive room membership. ([\#17692](https://github.com/element-hq/synapse/issues/17692))
- Fix bug where room account data would not correctly be sent down [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) Sliding Sync for old rooms. ([\#17695](https://github.com/element-hq/synapse/issues/17695))
- Fix a bug in [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) Sliding Sync which could prevent /sync from working for certain user accounts. ([\#17727](https://github.com/element-hq/synapse/issues/17727), [\#17733](https://github.com/element-hq/synapse/issues/17733))
- Ignore invites from ignored users in Sliding Sync. ([\#17729](https://github.com/element-hq/synapse/issues/17729))
- Fix bug in [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) Sliding Sync where the server would incorrectly return a negative bump stamp, which caused Element X apps to stop syncing. ([\#17748](https://github.com/element-hq/synapse/issues/17748))

### Internal Changes

- Import pydantic objects from the `_pydantic_compat` module.
  This allows `check_pydantic_models.py` to mock those pydantic objects
  only in the synapse module, and not interfere with pydantic objects in
  external dependencies. ([\#17667](https://github.com/element-hq/synapse/issues/17667))
- Use [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) Sliding Sync tables as a bulk shortcut for getting the max `event_stream_ordering` of rooms. ([\#17693](https://github.com/element-hq/synapse/issues/17693))
- Speed up [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) sliding sync requests a bit where there are many room changes. ([\#17696](https://github.com/element-hq/synapse/issues/17696))
- Refactor [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) sliding sync filter unit tests so the sliding sync API has better test coverage. ([\#17703](https://github.com/element-hq/synapse/issues/17703))
- Fetch `bump_stamp`s more efficiently in [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) Sliding Sync. ([\#17723](https://github.com/element-hq/synapse/issues/17723))
- Shortcut for checking if certain background updates have completed (utilized in [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) Sliding Sync). ([\#17724](https://github.com/element-hq/synapse/issues/17724))
- More efficiently fetch rooms for [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) Sliding Sync. ([\#17725](https://github.com/element-hq/synapse/issues/17725))
- Fix `_bulk_get_max_event_pos` being inefficient. ([\#17728](https://github.com/element-hq/synapse/issues/17728))
- Add cache to `get_tags_for_room(...)`. ([\#17730](https://github.com/element-hq/synapse/issues/17730))
- Small performance improvement in speeding up [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) Sliding Sync. ([\#17731](https://github.com/element-hq/synapse/issues/17731))
- Minor speed up of initial [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) sliding sync requests. ([\#17734](https://github.com/element-hq/synapse/issues/17734))
- Remove usage of the deprecated `cgi` module, deprecated in Python 3.11 and removed in Python 3.13. ([\#17741](https://github.com/element-hq/synapse/issues/17741))
- Fix typing of a variable that is not `Unknown` anymore after updating `treq`. ([\#17744](https://github.com/element-hq/synapse/issues/17744))



### Updates to locked dependencies

* Bump anyhow from 1.0.86 to 1.0.89. ([\#17685](https://github.com/element-hq/synapse/issues/17685), [\#17716](https://github.com/element-hq/synapse/issues/17716))
* Bump bytes from 1.7.1 to 1.7.2. ([\#17743](https://github.com/element-hq/synapse/issues/17743))
* Bump cryptography from 43.0.0 to 43.0.1. ([\#17689](https://github.com/element-hq/synapse/issues/17689))
* Bump idna from 3.8 to 3.10. ([\#17758](https://github.com/element-hq/synapse/issues/17758))
* Bump msgpack from 1.0.8 to 1.1.0. ([\#17759](https://github.com/element-hq/synapse/issues/17759))
* Bump phonenumbers from 8.13.44 to 8.13.45. ([\#17762](https://github.com/element-hq/synapse/issues/17762))
* Bump prometheus-client from 0.20.0 to 0.21.0. ([\#17746](https://github.com/element-hq/synapse/issues/17746))
* Bump pyasn1 from 0.6.0 to 0.6.1. ([\#17714](https://github.com/element-hq/synapse/issues/17714))
* Bump pyasn1-modules from 0.4.0 to 0.4.1. ([\#17747](https://github.com/element-hq/synapse/issues/17747))
* Bump pydantic from 2.8.2 to 2.9.2. ([\#17756](https://github.com/element-hq/synapse/issues/17756))
* Bump python-multipart from 0.0.9 to 0.0.10. ([\#17745](https://github.com/element-hq/synapse/issues/17745))
* Bump ruff from 0.6.4 to 0.6.7. ([\#17715](https://github.com/element-hq/synapse/issues/17715), [\#17760](https://github.com/element-hq/synapse/issues/17760))
* Bump sentry-sdk from 2.13.0 to 2.14.0. ([\#17712](https://github.com/element-hq/synapse/issues/17712))
* Bump serde from 1.0.209 to 1.0.210. ([\#17686](https://github.com/element-hq/synapse/issues/17686))
* Bump serde_json from 1.0.127 to 1.0.128. ([\#17687](https://github.com/element-hq/synapse/issues/17687))
* Bump treq from 23.11.0 to 24.9.1. ([\#17744](https://github.com/element-hq/synapse/issues/17744))
* Bump types-pyyaml from 6.0.12.20240808 to 6.0.12.20240917. ([\#17755](https://github.com/element-hq/synapse/issues/17755))
* Bump types-requests from 2.32.0.20240712 to 2.32.0.20240914. ([\#17713](https://github.com/element-hq/synapse/issues/17713))
* Bump types-setuptools from 74.1.0.20240907 to 75.1.0.20240917. ([\#17757](https://github.com/element-hq/synapse/issues/17757))

# Synapse 1.115.0 (2024-09-17)

No significant changes since 1.115.0rc2.




# Synapse 1.115.0rc2 (2024-09-12)

### Internal Changes

- Pre-populate room data used in experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint for quick filtering/sorting. ([\#17652](https://github.com/element-hq/synapse/issues/17652))
- Speed up sliding sync by reducing amount of data pulled out of the database for large rooms. ([\#17683](https://github.com/element-hq/synapse/issues/17683))




# Synapse 1.115.0rc1 (2024-09-10)

### Features

- Improve cross-signing upload when using [MSC3861](https://github.com/matrix-org/matrix-spec-proposals/pull/3861) to use a custom UIA flow stage, with web fallback support. ([\#17509](https://github.com/element-hq/synapse/issues/17509))

### Bugfixes

- Return `400 M_BAD_JSON` upon attempting to complete various room actions with a non-local user ID and unknown room ID, rather than an internal server error. ([\#17607](https://github.com/element-hq/synapse/issues/17607))
- Fix authenticated media responses using a wrong limit when following redirects over federation. ([\#17626](https://github.com/element-hq/synapse/issues/17626))
- Fix bug where we returned the wrong `bump_stamp` for invites in sliding sync response, causing incorrect ordering of invites in the room list. ([\#17674](https://github.com/element-hq/synapse/issues/17674))

### Improved Documentation

- Clarify that the admin api resource is only loaded on the main process and not workers. ([\#17590](https://github.com/element-hq/synapse/issues/17590))
- Fixed typo in `saml2_config` config [example](https://element-hq.github.io/synapse/latest/usage/configuration/config_documentation.html#saml2_config). ([\#17594](https://github.com/element-hq/synapse/issues/17594))

### Deprecations and Removals

- Stabilise [MSC4156](https://github.com/matrix-org/matrix-spec-proposals/pull/4156) by removing the `msc4156_enabled` config setting and defaulting it to `true`. ([\#17650](https://github.com/element-hq/synapse/issues/17650))

### Internal Changes

- Update [MSC3861](https://github.com/matrix-org/matrix-spec-proposals/pull/3861) implementation: load the issuer and account management URLs from OIDC discovery. ([\#17407](https://github.com/element-hq/synapse/issues/17407))
- Pre-populate room data used in experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint for quick filtering/sorting. ([\#17512](https://github.com/element-hq/synapse/issues/17512), [\#17632](https://github.com/element-hq/synapse/issues/17632), [\#17633](https://github.com/element-hq/synapse/issues/17633), [\#17634](https://github.com/element-hq/synapse/issues/17634), [\#17635](https://github.com/element-hq/synapse/issues/17635), [\#17636](https://github.com/element-hq/synapse/issues/17636), [\#17641](https://github.com/element-hq/synapse/issues/17641), [\#17654](https://github.com/element-hq/synapse/issues/17654), [\#17673](https://github.com/element-hq/synapse/issues/17673))
- Store sliding sync per-connection state in the database. ([\#17599](https://github.com/element-hq/synapse/issues/17599), [\#17631](https://github.com/element-hq/synapse/issues/17631))
- Make the sliding sync `PerConnectionState` class immutable. ([\#17600](https://github.com/element-hq/synapse/issues/17600))
- Replace `isort` and `black` with `ruff`. ([\#17620](https://github.com/element-hq/synapse/issues/17620), [\#17643](https://github.com/element-hq/synapse/issues/17643))
- Sliding Sync: Split up `get_room_membership_for_user_at_to_token`. ([\#17629](https://github.com/element-hq/synapse/issues/17629))
- Use new database tables for sliding sync. ([\#17630](https://github.com/element-hq/synapse/issues/17630), [\#17649](https://github.com/element-hq/synapse/issues/17649))
- Prevent duplicate tags being added to Sliding Sync traces. ([\#17655](https://github.com/element-hq/synapse/issues/17655))
- Get `bump_stamp` from [new sliding sync tables](https://github.com/element-hq/synapse/pull/17512) which should be faster. ([\#17658](https://github.com/element-hq/synapse/issues/17658))
- Speed up incremental Sliding Sync requests by avoiding extra work. ([\#17665](https://github.com/element-hq/synapse/issues/17665))
- Small performance improvement in speeding up sliding sync. ([\#17666](https://github.com/element-hq/synapse/issues/17666), [\#17670](https://github.com/element-hq/synapse/issues/17670), [\#17672](https://github.com/element-hq/synapse/issues/17672))
- Speed up sliding sync by reducing number of database calls. ([\#17684](https://github.com/element-hq/synapse/issues/17684))
- Speed up sync by pulling out fewer events from the database. ([\#17688](https://github.com/element-hq/synapse/issues/17688))



### Updates to locked dependencies

* Bump authlib from 1.3.1 to 1.3.2. ([\#17679](https://github.com/element-hq/synapse/issues/17679))
* Bump idna from 3.7 to 3.8. ([\#17682](https://github.com/element-hq/synapse/issues/17682))
* Bump ruff from 0.6.2 to 0.6.4. ([\#17680](https://github.com/element-hq/synapse/issues/17680))
* Bump towncrier from 24.7.1 to 24.8.0. ([\#17645](https://github.com/element-hq/synapse/issues/17645))
* Bump twisted from 24.7.0rc1 to 24.7.0. ([\#17647](https://github.com/element-hq/synapse/issues/17647))
* Bump types-pillow from 10.2.0.20240520 to 10.2.0.20240822. ([\#17644](https://github.com/element-hq/synapse/issues/17644))
* Bump types-psycopg2 from 2.9.21.20240417 to 2.9.21.20240819. ([\#17646](https://github.com/element-hq/synapse/issues/17646))
* Bump types-setuptools from 71.1.0.20240818 to 74.1.0.20240907. ([\#17681](https://github.com/element-hq/synapse/issues/17681))

# Synapse 1.114.0 (2024-09-02)

This release enables support for
[MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186) —
Simplified Sliding Sync. This allows using the upcoming releases of the Element
X mobile apps without having to run a Sliding Sync Proxy.


### Features

- Enable native sliding sync support ([MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) and [MSC4186](https://github.com/matrix-org/matrix-spec-proposals/pull/4186)) by default. ([\#17648](https://github.com/element-hq/synapse/issues/17648))




# Synapse 1.114.0rc3 (2024-08-30)

### Bugfixes

- Fix regression in v1.114.0rc2 that caused workers to fail to start. ([\#17626](https://github.com/element-hq/synapse/issues/17626))




# Synapse 1.114.0rc2 (2024-08-30)

### Features

- Improve cross-signing upload when using [MSC3861](https://github.com/matrix-org/matrix-spec-proposals/pull/3861) to use a custom UIA flow stage, with web fallback support. ([\#17509](https://github.com/element-hq/synapse/issues/17509))
- Make `hash_password` script accept password input from stdin. ([\#17608](https://github.com/element-hq/synapse/issues/17608))

### Bugfixes

- Fix hierarchy returning 403 when room is accessible through federation. Contributed by Krishan (@kfiven). ([\#17194](https://github.com/element-hq/synapse/issues/17194))
- Fix content-length on federation `/thumbnail` responses. ([\#17532](https://github.com/element-hq/synapse/issues/17532))
- Fix authenticated media responses using a wrong limit when following redirects over federation. ([\#17543](https://github.com/element-hq/synapse/issues/17543))

### Internal Changes

- MSC3861: load the issuer and account management URLs from OIDC discovery. ([\#17407](https://github.com/element-hq/synapse/issues/17407))
- Refactor sliding sync class into multiple files. ([\#17595](https://github.com/element-hq/synapse/issues/17595))
- Store sliding sync per-connection state in the database. ([\#17599](https://github.com/element-hq/synapse/issues/17599))
- Make the sliding sync `PerConnectionState` class immutable. ([\#17600](https://github.com/element-hq/synapse/issues/17600))
- Add support to `@tag_args` for standalone functions. ([\#17604](https://github.com/element-hq/synapse/issues/17604))
- Speed up incremental syncs in sliding sync by adding some more caching. ([\#17606](https://github.com/element-hq/synapse/issues/17606))
- Always return the user's own read receipts in sliding sync. ([\#17617](https://github.com/element-hq/synapse/issues/17617))
- Replace `isort` and `black` with `ruff`. ([\#17620](https://github.com/element-hq/synapse/issues/17620))
- Refactor sliding sync code to move room list logic out into a separate class. ([\#17622](https://github.com/element-hq/synapse/issues/17622))



### Updates to locked dependencies

* Bump attrs from 23.2.0 to 24.2.0. ([\#17609](https://github.com/element-hq/synapse/issues/17609))
* Bump cryptography from 42.0.8 to 43.0.0. ([\#17584](https://github.com/element-hq/synapse/issues/17584))
* Bump phonenumbers from 8.13.43 to 8.13.44. ([\#17610](https://github.com/element-hq/synapse/issues/17610))
* Bump pygithub from 2.3.0 to 2.4.0. ([\#17612](https://github.com/element-hq/synapse/issues/17612))
* Bump pyyaml from 6.0.1 to 6.0.2. ([\#17611](https://github.com/element-hq/synapse/issues/17611))
* Bump sentry-sdk from 2.12.0 to 2.13.0. ([\#17585](https://github.com/element-hq/synapse/issues/17585))
* Bump serde from 1.0.206 to 1.0.208. ([\#17581](https://github.com/element-hq/synapse/issues/17581))
* Bump serde from 1.0.208 to 1.0.209. ([\#17613](https://github.com/element-hq/synapse/issues/17613))
* Bump serde_json from 1.0.124 to 1.0.125. ([\#17582](https://github.com/element-hq/synapse/issues/17582))
* Bump serde_json from 1.0.125 to 1.0.127. ([\#17614](https://github.com/element-hq/synapse/issues/17614))
* Bump types-jsonschema from 4.23.0.20240712 to 4.23.0.20240813. ([\#17583](https://github.com/element-hq/synapse/issues/17583))
* Bump types-setuptools from 71.1.0.20240726 to 71.1.0.20240818. ([\#17586](https://github.com/element-hq/synapse/issues/17586))

# Synapse 1.114.0rc1 (2024-08-20)

### Features

- Add a flag to `/versions`, `org.matrix.simplified_msc3575`, to indicate whether experimental sliding sync support has been enabled. ([\#17571](https://github.com/element-hq/synapse/issues/17571))
- Handle changes in `timeline_limit` in experimental sliding sync. ([\#17579](https://github.com/element-hq/synapse/issues/17579))
- Correctly track read receipts that should be sent down in experimental sliding sync. ([\#17575](https://github.com/element-hq/synapse/issues/17575), [\#17589](https://github.com/element-hq/synapse/issues/17589), [\#17592](https://github.com/element-hq/synapse/issues/17592))

### Bugfixes

- Start handlers for new media endpoints when media resource configured. ([\#17483](https://github.com/element-hq/synapse/issues/17483))
- Fix timeline ordering (using `stream_ordering` instead of topological ordering) in experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17510](https://github.com/element-hq/synapse/issues/17510))
- Fix experimental sliding sync implementation to remember any updates in rooms that were not sent down immediately. ([\#17535](https://github.com/element-hq/synapse/issues/17535))
- Better exclude partially stated rooms if we must await full state in experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17538](https://github.com/element-hq/synapse/issues/17538))
- Handle lower-case http headers in `_Mulitpart_Parser_Protocol`. ([\#17545](https://github.com/element-hq/synapse/issues/17545))
- Fix fetching federation signing keys from servers that omit `old_verify_keys`. Contributed by @tulir @ Beeper. ([\#17568](https://github.com/element-hq/synapse/issues/17568))
- Fix bug where we would respond with an error when a remote server asked for media that had a length of 0, using the new multipart federation media endpoint. ([\#17570](https://github.com/element-hq/synapse/issues/17570))

### Improved Documentation

- Clarify default behaviour of the
  [`auto_accept_invites.worker_to_run_on`](https://element-hq.github.io/synapse/develop/usage/configuration/config_documentation.html#auto-accept-invites)
  option. ([\#17515](https://github.com/element-hq/synapse/issues/17515))
- Improve docstrings for profile methods. ([\#17559](https://github.com/element-hq/synapse/issues/17559))

### Internal Changes

- Add more tracing to experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17514](https://github.com/element-hq/synapse/issues/17514))
- Fixup comment in sliding sync implementation. ([\#17531](https://github.com/element-hq/synapse/issues/17531))
- Replace override of deprecated method `HTTPAdapter.get_connection` with `get_connection_with_tls_context`. ([\#17536](https://github.com/element-hq/synapse/issues/17536))
- Fix performance of device lists in `/key/changes` and sliding sync. ([\#17537](https://github.com/element-hq/synapse/issues/17537), [\#17548](https://github.com/element-hq/synapse/issues/17548))
- Bump setuptools from 67.6.0 to 72.1.0. ([\#17542](https://github.com/element-hq/synapse/issues/17542))
- Add a utility function for generating random event IDs. ([\#17557](https://github.com/element-hq/synapse/issues/17557))
- Speed up responding to media requests. ([\#17558](https://github.com/element-hq/synapse/issues/17558), [\#17561](https://github.com/element-hq/synapse/issues/17561), [\#17564](https://github.com/element-hq/synapse/issues/17564), [\#17566](https://github.com/element-hq/synapse/issues/17566), [\#17567](https://github.com/element-hq/synapse/issues/17567), [\#17569](https://github.com/element-hq/synapse/issues/17569))
- Test github token before running release script steps. ([\#17562](https://github.com/element-hq/synapse/issues/17562))
- Reduce log spam of multipart files. ([\#17563](https://github.com/element-hq/synapse/issues/17563))
- Refactor per-connection state in experimental sliding sync handler. ([\#17574](https://github.com/element-hq/synapse/issues/17574))
- Add histogram metrics for sliding sync processing time. ([\#17593](https://github.com/element-hq/synapse/issues/17593))



### Updates to locked dependencies

* Bump bytes from 1.6.1 to 1.7.1. ([\#17526](https://github.com/element-hq/synapse/issues/17526))
* Bump lxml from 5.2.2 to 5.3.0. ([\#17550](https://github.com/element-hq/synapse/issues/17550))
* Bump phonenumbers from 8.13.42 to 8.13.43. ([\#17551](https://github.com/element-hq/synapse/issues/17551))
* Bump regex from 1.10.5 to 1.10.6. ([\#17527](https://github.com/element-hq/synapse/issues/17527))
* Bump sentry-sdk from 2.10.0 to 2.12.0. ([\#17553](https://github.com/element-hq/synapse/issues/17553))
* Bump serde from 1.0.204 to 1.0.206. ([\#17556](https://github.com/element-hq/synapse/issues/17556))
* Bump serde_json from 1.0.122 to 1.0.124. ([\#17555](https://github.com/element-hq/synapse/issues/17555))
* Bump sigstore/cosign-installer from 3.5.0 to 3.6.0. ([\#17549](https://github.com/element-hq/synapse/issues/17549))
* Bump types-pyyaml from 6.0.12.20240311 to 6.0.12.20240808. ([\#17552](https://github.com/element-hq/synapse/issues/17552))
* Bump types-requests from 2.31.0.20240406 to 2.32.0.20240712. ([\#17524](https://github.com/element-hq/synapse/issues/17524))

# Synapse 1.113.0 (2024-08-13)

No significant changes since 1.113.0rc1.




# Synapse 1.113.0rc1 (2024-08-06)

### Features

- Track which rooms have been sent to clients in the experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17447](https://github.com/element-hq/synapse/issues/17447))
- Add Account Data extension support to experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17477](https://github.com/element-hq/synapse/issues/17477))
- Add receipts extension support to experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17489](https://github.com/element-hq/synapse/issues/17489))
- Add typing notification extension support to experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17505](https://github.com/element-hq/synapse/issues/17505))

### Bugfixes

- Update experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint to handle invite/knock rooms when filtering. ([\#17450](https://github.com/element-hq/synapse/issues/17450))
- Fix a bug introduced in v1.110.0 which caused `/keys/query` to return incomplete results, leading to high network activity and CPU usage on Matrix clients. ([\#17499](https://github.com/element-hq/synapse/issues/17499))

### Improved Documentation

- Update the [`allowed_local_3pids`](https://element-hq.github.io/synapse/v1.112/usage/configuration/config_documentation.html#allowed_local_3pids) config option's msisdn address to a working example. ([\#17476](https://github.com/element-hq/synapse/issues/17476))

### Internal Changes

- Change sliding sync to use their own token format in preparation for storing per-connection state. ([\#17452](https://github.com/element-hq/synapse/issues/17452))
- Ensure we don't send down negative `bump_stamp` in experimental sliding sync endpoint. ([\#17478](https://github.com/element-hq/synapse/issues/17478))
- Do not send down empty room entries down experimental sliding sync endpoint. ([\#17479](https://github.com/element-hq/synapse/issues/17479))
- Refactor Sliding Sync tests to better utilize the `SlidingSyncBase`. ([\#17481](https://github.com/element-hq/synapse/issues/17481), [\#17482](https://github.com/element-hq/synapse/issues/17482))
- Add some opentracing tags and logging to the experimental sliding sync implementation. ([\#17501](https://github.com/element-hq/synapse/issues/17501))
- Split and move Sliding Sync tests so we have some more sane test file sizes. ([\#17504](https://github.com/element-hq/synapse/issues/17504))
- Update the `limited` field description in the Sliding Sync response to accurately describe what it actually represents. ([\#17507](https://github.com/element-hq/synapse/issues/17507))
- Easier to understand `timeline` assertions in Sliding Sync tests. ([\#17511](https://github.com/element-hq/synapse/issues/17511))
- Reset the sliding sync connection if we don't recognize the per-connection state position. ([\#17529](https://github.com/element-hq/synapse/issues/17529))



### Updates to locked dependencies

* Bump bcrypt from 4.1.3 to 4.2.0. ([\#17495](https://github.com/element-hq/synapse/issues/17495))
* Bump black from 24.4.2 to 24.8.0. ([\#17522](https://github.com/element-hq/synapse/issues/17522))
* Bump phonenumbers from 8.13.39 to 8.13.42. ([\#17521](https://github.com/element-hq/synapse/issues/17521))
* Bump ruff from 0.5.4 to 0.5.5. ([\#17494](https://github.com/element-hq/synapse/issues/17494))
* Bump serde_json from 1.0.120 to 1.0.121. ([\#17493](https://github.com/element-hq/synapse/issues/17493))
* Bump serde_json from 1.0.121 to 1.0.122. ([\#17525](https://github.com/element-hq/synapse/issues/17525))
* Bump towncrier from 23.11.0 to 24.7.1. ([\#17523](https://github.com/element-hq/synapse/issues/17523))
* Bump types-pyopenssl from 24.1.0.20240425 to 24.1.0.20240722. ([\#17496](https://github.com/element-hq/synapse/issues/17496))
* Bump types-setuptools from 70.1.0.20240627 to 71.1.0.20240726. ([\#17497](https://github.com/element-hq/synapse/issues/17497))

# Synapse 1.112.0 (2024-07-30)

This security release is to update our locked dependency on Twisted to 24.7.0rc1, which includes a security fix for [CVE-2024-41671 / GHSA-c8m8-j448-xjx7: Disordered HTTP pipeline response in twisted.web, again](https://github.com/twisted/twisted/security/advisories/GHSA-c8m8-j448-xjx7).

Note that this security fix is also available as **Synapse 1.111.1**, which does not include the rest of the changes in Synapse 1.112.0.

This issue means that, if multiple HTTP requests are pipelined in the same TCP connection, Synapse can send responses to the wrong HTTP request.
If a reverse proxy was configured to use HTTP pipelining, this could result in responses being sent to the wrong user, severely harming confidentiality.

With that said, despite being a high severity issue, **we consider it unlikely that Synapse installations will be affected**.
The use of HTTP pipelining in this fashion would cause worse performance for clients (request-response latencies would be increased as users' responses would be artificially blocked behind other users' slow requests). Further, Nginx and Haproxy, two common reverse proxies, do not appear to support configuring their upstreams to use HTTP pipelining and thus would not be affected. For both of these reasons, we consider it unlikely that a Synapse deployment would be set up in such a configuration.

Despite that, we cannot rule out that some installations may exist with this unusual setup and so we are releasing this security update today.

**pip users:** Note that by default, upgrading Synapse using pip will not automatically upgrade Twisted. **Please manually install the new version of Twisted** using `pip install Twisted==24.7.0rc1`. Note also that even the `--upgrade-strategy=eager` flag to `pip install -U matrix-synapse` will not upgrade Twisted to a patched version because it is only a release candidate at this time.

### Internal Changes

- Upgrade locked dependency on Twisted to 24.7.0rc1. ([\#17502](https://github.com/element-hq/synapse/issues/17502))


# Synapse 1.112.0rc1 (2024-07-23)

Please note that this release candidate does not include the security dependency update
included in version 1.111.1 as this version was released before 1.111.1.
The same security fix can be found in the full release of 1.112.0.

### Features

- Add to-device extension support to experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17416](https://github.com/element-hq/synapse/issues/17416))
- Populate `name`/`avatar` fields in experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17418](https://github.com/element-hq/synapse/issues/17418))
- Populate `heroes` and room summary fields (`joined_count`, `invited_count`) in experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17419](https://github.com/element-hq/synapse/issues/17419))
- Populate `is_dm` room field in experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17429](https://github.com/element-hq/synapse/issues/17429))
- Add room subscriptions to experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17432](https://github.com/element-hq/synapse/issues/17432))
- Prepare for authenticated media freeze. ([\#17433](https://github.com/element-hq/synapse/issues/17433))
- Add E2EE extension support to experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17454](https://github.com/element-hq/synapse/issues/17454))

### Bugfixes

- Add configurable option to always include offline users in presence sync results. Contributed by @Michael-Hollister. ([\#17231](https://github.com/element-hq/synapse/issues/17231))
- Fix bug in experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint when using room type filters and the user has one or more remote invites. ([\#17434](https://github.com/element-hq/synapse/issues/17434))
- Order `heroes` by `stream_ordering` as the Matrix specification states (applies to `/sync`). ([\#17435](https://github.com/element-hq/synapse/issues/17435))
- Fix rare bug where `/sync` would break for a user when using workers with multiple stream writers. ([\#17438](https://github.com/element-hq/synapse/issues/17438))

### Improved Documentation

- Update the readme image to have a white background, so that it is readable in dark mode. ([\#17387](https://github.com/element-hq/synapse/issues/17387))
- Add Red Hat Enterprise Linux and Rocky Linux 8 and 9 installation instructions. ([\#17423](https://github.com/element-hq/synapse/issues/17423))
- Improve documentation for the [`default_power_level_content_override`](https://element-hq.github.io/synapse/latest/usage/configuration/config_documentation.html#default_power_level_content_override) config option. ([\#17451](https://github.com/element-hq/synapse/issues/17451))

### Internal Changes

- Make sure we always use the right logic for enabling the media repo. ([\#17424](https://github.com/element-hq/synapse/issues/17424))
- Fix argument documentation for method `RateLimiter.record_action`. ([\#17426](https://github.com/element-hq/synapse/issues/17426))
- Reduce volume of 'Waiting for current token' logs, which were introduced in v1.109.0. ([\#17428](https://github.com/element-hq/synapse/issues/17428))
- Limit concurrent remote downloads to 6 per IP address, and decrement remote downloads without a content-length from the ratelimiter after the download is complete. ([\#17439](https://github.com/element-hq/synapse/issues/17439))
- Remove unnecessary call to resume producing in fake channel. ([\#17449](https://github.com/element-hq/synapse/issues/17449))
- Update experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint to bump room when it is created. ([\#17453](https://github.com/element-hq/synapse/issues/17453))
- Speed up generating sliding sync responses. ([\#17458](https://github.com/element-hq/synapse/issues/17458))
- Add cache to `get_rooms_for_local_user_where_membership_is` to speed up sliding sync. ([\#17460](https://github.com/element-hq/synapse/issues/17460))
- Speed up fetching room keys from backup. ([\#17461](https://github.com/element-hq/synapse/issues/17461))
- Speed up sorting of the room list in sliding sync. ([\#17468](https://github.com/element-hq/synapse/issues/17468))
- Implement handling of `$ME` as a state key in sliding sync. ([\#17469](https://github.com/element-hq/synapse/issues/17469))



### Updates to locked dependencies

* Bump bytes from 1.6.0 to 1.6.1. ([\#17441](https://github.com/element-hq/synapse/issues/17441))
* Bump hiredis from 2.3.2 to 3.0.0. ([\#17464](https://github.com/element-hq/synapse/issues/17464))
* Bump jsonschema from 4.22.0 to 4.23.0. ([\#17444](https://github.com/element-hq/synapse/issues/17444))
* Bump matrix-org/done-action from 2 to 3. ([\#17440](https://github.com/element-hq/synapse/issues/17440))
* Bump mypy from 1.9.0 to 1.10.1. ([\#17445](https://github.com/element-hq/synapse/issues/17445))
* Bump pyopenssl from 24.1.0 to 24.2.1. ([\#17465](https://github.com/element-hq/synapse/issues/17465))
* Bump ruff from 0.5.0 to 0.5.4. ([\#17466](https://github.com/element-hq/synapse/issues/17466))
* Bump sentry-sdk from 2.6.0 to 2.8.0. ([\#17456](https://github.com/element-hq/synapse/issues/17456))
* Bump sentry-sdk from 2.8.0 to 2.10.0. ([\#17467](https://github.com/element-hq/synapse/issues/17467))
* Bump setuptools from 67.6.0 to 70.0.0. ([\#17448](https://github.com/element-hq/synapse/issues/17448))
* Bump twine from 5.1.0 to 5.1.1. ([\#17443](https://github.com/element-hq/synapse/issues/17443))
* Bump types-jsonschema from 4.22.0.20240610 to 4.23.0.20240712. ([\#17446](https://github.com/element-hq/synapse/issues/17446))
* Bump ulid from 1.1.2 to 1.1.3. ([\#17442](https://github.com/element-hq/synapse/issues/17442))
* Bump zipp from 3.15.0 to 3.19.1. ([\#17427](https://github.com/element-hq/synapse/issues/17427))


# Synapse 1.111.1 (2024-07-30)

This security release is to update our locked dependency on Twisted to 24.7.0rc1, which includes a security fix for [CVE-2024-41671 / GHSA-c8m8-j448-xjx7: Disordered HTTP pipeline response in twisted.web, again](https://github.com/twisted/twisted/security/advisories/GHSA-c8m8-j448-xjx7).

This issue means that, if multiple HTTP requests are pipelined in the same TCP connection, Synapse can send responses to the wrong HTTP request.
If a reverse proxy was configured to use HTTP pipelining, this could result in responses being sent to the wrong user, severely harming confidentiality.

With that said, despite being a high severity issue, **we consider it unlikely that Synapse installations will be affected**.
The use of HTTP pipelining in this fashion would cause worse performance for clients (request-response latencies would be increased as users' responses would be artificially blocked behind other users' slow requests). Further, Nginx and Haproxy, two common reverse proxies, do not appear to support configuring their upstreams to use HTTP pipelining and thus would not be affected. For both of these reasons, we consider it unlikely that a Synapse deployment would be set up in such a configuration.

Despite that, we cannot rule out that some installations may exist with this unusual setup and so we are releasing this security update today.

**pip users:** Note that by default, upgrading Synapse using pip will not automatically upgrade Twisted. **Please manually install the new version of Twisted** using `pip install Twisted==24.7.0rc1`. Note also that even the `--upgrade-strategy=eager` flag to `pip install -U matrix-synapse` will not upgrade Twisted to a patched version because it is only a release candidate at this time.


### Internal Changes

- Upgrade locked dependency on Twisted to 24.7.0rc1. ([\#17502](https://github.com/element-hq/synapse/issues/17502))


# Synapse 1.111.0 (2024-07-16)

No significant changes since 1.111.0rc2.




# Synapse 1.111.0rc2 (2024-07-10)

### Bugfixes

- Fix bug where using `synapse.app.media_repository` worker configuration would break the new media endpoints. ([\#17420](https://github.com/element-hq/synapse/issues/17420))

### Improved Documentation

- Document the new federation media worker endpoints in the [upgrade notes](https://element-hq.github.io/synapse/v1.111/upgrade.html) and [worker docs](https://element-hq.github.io/synapse/v1.111/workers.html). ([\#17421](https://github.com/element-hq/synapse/issues/17421))

### Internal Changes

- Route authenticated federation media requests to media repository workers in Complement tests. ([\#17422](https://github.com/element-hq/synapse/issues/17422))




# Synapse 1.111.0rc1 (2024-07-09)

### Features

- Add `rooms` data to experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17320](https://github.com/element-hq/synapse/issues/17320))
- Add `room_types`/`not_room_types` filtering to experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17337](https://github.com/element-hq/synapse/issues/17337))
- Return "required state" in experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17342](https://github.com/element-hq/synapse/issues/17342))
- Support [MSC3916](https://github.com/matrix-org/matrix-spec-proposals/blob/main/proposals/3916-authentication-for-media.md) by adding [`_matrix/client/v1/media/download`](https://spec.matrix.org/v1.11/client-server-api/#get_matrixclientv1mediadownloadservernamemediaid) endpoint. ([\#17365](https://github.com/element-hq/synapse/issues/17365))
- Support [MSC3916](https://github.com/matrix-org/matrix-spec-proposals/blob/rav/authentication-for-media/proposals/3916-authentication-for-media.md)
  by adding [`_matrix/client/v1/media/thumbnail`](https://spec.matrix.org/v1.11/client-server-api/#get_matrixclientv1mediathumbnailservernamemediaid), [`_matrix/federation/v1/media/thumbnail`](https://spec.matrix.org/v1.11/server-server-api/#get_matrixfederationv1mediathumbnailmediaid) endpoints and stabilizing the
  remaining [`_matrix/client/v1/media`](https://spec.matrix.org/v1.11/client-server-api/#get_matrixclientv1mediaconfig) endpoints. ([\#17388](https://github.com/element-hq/synapse/issues/17388))
- Add `rooms.bump_stamp` for easier client-side sorting in experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17395](https://github.com/element-hq/synapse/issues/17395))
- Forget all of a user's rooms upon deactivation, preventing local room purges from being blocked on deactivated users. ([\#17400](https://github.com/element-hq/synapse/issues/17400))
- Declare support for [Matrix 1.11](https://matrix.org/blog/2024/06/20/matrix-v1.11-release/). ([\#17403](https://github.com/element-hq/synapse/issues/17403))
- [MSC3861](https://github.com/matrix-org/matrix-spec-proposals/pull/3861): allow overriding the introspection endpoint. ([\#17406](https://github.com/element-hq/synapse/issues/17406))

### Bugfixes

- Fix rare race which caused no new to-device messages to be received from remote server. ([\#17362](https://github.com/element-hq/synapse/issues/17362))
- Fix bug in experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint when using an old database. ([\#17398](https://github.com/element-hq/synapse/issues/17398))

### Improved Documentation

- Clarify that `url_preview_url_blacklist` is a usability feature. ([\#17356](https://github.com/element-hq/synapse/issues/17356))
- Fix broken links in README. ([\#17379](https://github.com/element-hq/synapse/issues/17379))
- Clarify that changelog content *and file extension* need to match in order for entries to merge. ([\#17399](https://github.com/element-hq/synapse/issues/17399))

### Internal Changes

- Make the release script create a release branch for Complement as well. ([\#17318](https://github.com/element-hq/synapse/issues/17318))
- Fix uploading packages to PyPi. ([\#17363](https://github.com/element-hq/synapse/issues/17363))
- Add CI check for the README. ([\#17367](https://github.com/element-hq/synapse/issues/17367))
- Fix linting errors from new `ruff` version. ([\#17381](https://github.com/element-hq/synapse/issues/17381), [\#17411](https://github.com/element-hq/synapse/issues/17411))
- Fix building debian packages on non-clean checkouts. ([\#17390](https://github.com/element-hq/synapse/issues/17390))
- Finish up work to allow per-user feature flags. ([\#17392](https://github.com/element-hq/synapse/issues/17392), [\#17410](https://github.com/element-hq/synapse/issues/17410))
- Allow enabling sliding sync per-user. ([\#17393](https://github.com/element-hq/synapse/issues/17393))



### Updates to locked dependencies

* Bump certifi from 2023.7.22 to 2024.7.4. ([\#17404](https://github.com/element-hq/synapse/issues/17404))
* Bump cryptography from 42.0.7 to 42.0.8. ([\#17382](https://github.com/element-hq/synapse/issues/17382))
* Bump ijson from 3.2.3 to 3.3.0. ([\#17413](https://github.com/element-hq/synapse/issues/17413))
* Bump log from 0.4.21 to 0.4.22. ([\#17384](https://github.com/element-hq/synapse/issues/17384))
* Bump mypy-zope from 1.0.4 to 1.0.5. ([\#17414](https://github.com/element-hq/synapse/issues/17414))
* Bump pillow from 10.3.0 to 10.4.0. ([\#17412](https://github.com/element-hq/synapse/issues/17412))
* Bump pydantic from 2.7.1 to 2.8.2. ([\#17415](https://github.com/element-hq/synapse/issues/17415))
* Bump ruff from 0.3.7 to 0.5.0. ([\#17381](https://github.com/element-hq/synapse/issues/17381))
* Bump serde from 1.0.203 to 1.0.204. ([\#17409](https://github.com/element-hq/synapse/issues/17409))
* Bump serde_json from 1.0.117 to 1.0.120. ([\#17385](https://github.com/element-hq/synapse/issues/17385), [\#17408](https://github.com/element-hq/synapse/issues/17408))
* Bump types-setuptools from 69.5.0.20240423 to 70.1.0.20240627. ([\#17380](https://github.com/element-hq/synapse/issues/17380))

# Synapse 1.110.0 (2024-07-03)

No significant changes since 1.110.0rc3.




# Synapse 1.110.0rc3 (2024-07-02)

### Bugfixes

- Fix bug where `/sync` requests could get blocked indefinitely after an upgrade from Synapse versions before v1.109.0. ([\#17386](https://github.com/element-hq/synapse/issues/17386), [\#17391](https://github.com/element-hq/synapse/issues/17391))

### Internal Changes

- Limit size of presence EDUs to 50 entries. ([\#17371](https://github.com/element-hq/synapse/issues/17371))
- Fix building debian package for debian sid. ([\#17389](https://github.com/element-hq/synapse/issues/17389))




# Synapse 1.110.0rc2 (2024-06-26)

### Internal Changes

- Fix uploading packages to PyPi. ([\#17363](https://github.com/element-hq/synapse/issues/17363))




# Synapse 1.110.0rc1 (2024-06-26)

### Features

- Add initial implementation of an experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17187](https://github.com/element-hq/synapse/issues/17187))
- Add experimental support for [MSC3823](https://github.com/matrix-org/matrix-spec-proposals/pull/3823) - Account suspension. ([\#17255](https://github.com/element-hq/synapse/issues/17255))
- Improve ratelimiting in Synapse. ([\#17256](https://github.com/element-hq/synapse/issues/17256))
- Add support for the unstable [MSC4151](https://github.com/matrix-org/matrix-spec-proposals/pull/4151) report room API. ([\#17270](https://github.com/element-hq/synapse/issues/17270), [\#17296](https://github.com/element-hq/synapse/issues/17296))
- Filter for public and empty rooms added to Admin-API [List Room API](https://element-hq.github.io/synapse/latest/admin_api/rooms.html#list-room-api). ([\#17276](https://github.com/element-hq/synapse/issues/17276))
- Add `is_dm` filtering to experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17277](https://github.com/element-hq/synapse/issues/17277))
- Add `is_encrypted` filtering to experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17281](https://github.com/element-hq/synapse/issues/17281))
- Include user membership in events served to clients, per [MSC4115](https://github.com/matrix-org/matrix-spec-proposals/pull/4115). ([\#17282](https://github.com/element-hq/synapse/issues/17282))
- Do not require user-interactive authentication for uploading cross-signing keys for the first time, per [MSC3967](https://github.com/matrix-org/matrix-spec-proposals/pull/3967). ([\#17284](https://github.com/element-hq/synapse/issues/17284))
- Add `stream_ordering` sort to experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17293](https://github.com/element-hq/synapse/issues/17293))
- `register_new_matrix_user` now supports a --password-file flag, which
  is useful for scripting. ([\#17294](https://github.com/element-hq/synapse/issues/17294))
- `register_new_matrix_user` now supports a --exists-ok flag to allow registration of users that already exist in the database.
  This is useful for scripts that bootstrap user accounts with initial passwords. ([\#17304](https://github.com/element-hq/synapse/issues/17304))
- Add support for via query parameter from [MSC4156](https://github.com/matrix-org/matrix-spec-proposals/pull/4156). ([\#17322](https://github.com/element-hq/synapse/issues/17322))
- Add `is_invite` filtering to experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17335](https://github.com/element-hq/synapse/issues/17335))
- Support [MSC3916](https://github.com/matrix-org/matrix-spec-proposals/blob/main/proposals/3916-authentication-for-media.md) by adding a federation /download endpoint. ([\#17350](https://github.com/element-hq/synapse/issues/17350))

### Bugfixes

- Fix searching for users with their exact localpart whose ID includes a hyphen. ([\#17254](https://github.com/element-hq/synapse/issues/17254))
- Fix wrong retention policy being used when filtering events. ([\#17272](https://github.com/element-hq/synapse/issues/17272))
- Fix bug where OTKs were not always included in `/sync` response when using workers. ([\#17275](https://github.com/element-hq/synapse/issues/17275))
- Fix a long-standing bug where an invalid 'from' parameter to [`/notifications`](https://spec.matrix.org/v1.10/client-server-api/#get_matrixclientv3notifications) would result in an Internal Server Error. ([\#17283](https://github.com/element-hq/synapse/issues/17283))
- Fix edge case in `/sync` returning the wrong the state when using sharded event persisters. ([\#17295](https://github.com/element-hq/synapse/issues/17295))
- Add initial implementation of an experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync` endpoint. ([\#17301](https://github.com/element-hq/synapse/issues/17301))
- Fix email notification subject when invited to a space. ([\#17336](https://github.com/element-hq/synapse/issues/17336))

### Improved Documentation

- Add missing quotes for example for `exclude_rooms_from_sync`. ([\#17308](https://github.com/element-hq/synapse/issues/17308))
- Update header in the README to visually fix the the auto-generated table of contents. ([\#17329](https://github.com/element-hq/synapse/issues/17329))
- Fix stale references to the Foundation's Security Disclosure Policy. ([\#17341](https://github.com/element-hq/synapse/issues/17341))
- Add default values for `rc_invites.per_issuer` to docs. ([\#17347](https://github.com/element-hq/synapse/issues/17347))
- Fix an error in the docs for `search_all_users` parameter under `user_directory`. ([\#17348](https://github.com/element-hq/synapse/issues/17348))

### Internal Changes

- Remove unused `expire_access_token` option in the Synapse Docker config file. Contributed by @AaronDewes. ([\#17198](https://github.com/element-hq/synapse/issues/17198))
- Use fully-qualified `PersistedEventPosition` when returning `RoomsForUser` to facilitate proper comparisons and `RoomStreamToken` generation. ([\#17265](https://github.com/element-hq/synapse/issues/17265))
- Add debug logging for when room keys are uploaded, including whether they are replacing other room keys. ([\#17266](https://github.com/element-hq/synapse/issues/17266))
- Handle OTK uploads off master. ([\#17271](https://github.com/element-hq/synapse/issues/17271))
- Don't try and resync devices for remote users whose servers are marked as down. ([\#17273](https://github.com/element-hq/synapse/issues/17273))
- Re-organize Pydantic models and types used in handlers. ([\#17279](https://github.com/element-hq/synapse/issues/17279))
- Expose the worker instance that persisted the event on `event.internal_metadata.instance_name`. ([\#17300](https://github.com/element-hq/synapse/issues/17300))
- Update the README with Element branding, improve headers and fix the #synapse:matrix.org support room link rendering. ([\#17324](https://github.com/element-hq/synapse/issues/17324))
- Change path of the experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync implementation to `/org.matrix.simplified_msc3575/sync` since our simplified API is slightly incompatible with what's in the current MSC. ([\#17331](https://github.com/element-hq/synapse/issues/17331))
- Handle device lists notifications for large accounts more efficiently in worker mode. ([\#17333](https://github.com/element-hq/synapse/issues/17333), [\#17358](https://github.com/element-hq/synapse/issues/17358))
- Do not block event sending/receiving while calculating large event auth chains. ([\#17338](https://github.com/element-hq/synapse/issues/17338))
- Tidy up `parse_integer` docs and call sites to reflect the fact that they require non-negative integers by default, and bring `parse_integer_from_args` default in alignment. Contributed by Denis Kasak (@dkasak). ([\#17339](https://github.com/element-hq/synapse/issues/17339))



### Updates to locked dependencies

* Bump authlib from 1.3.0 to 1.3.1. ([\#17343](https://github.com/element-hq/synapse/issues/17343))
* Bump dawidd6/action-download-artifact from 3.1.4 to 5. ([\#17289](https://github.com/element-hq/synapse/issues/17289))
* Bump dawidd6/action-download-artifact from 5 to 6. ([\#17313](https://github.com/element-hq/synapse/issues/17313))
* Bump docker/build-push-action from 5 to 6. ([\#17312](https://github.com/element-hq/synapse/issues/17312))
* Bump jinja2 from 3.1.3 to 3.1.4. ([\#17287](https://github.com/element-hq/synapse/issues/17287))
* Bump lazy_static from 1.4.0 to 1.5.0. ([\#17355](https://github.com/element-hq/synapse/issues/17355))
* Bump msgpack from 1.0.7 to 1.0.8. ([\#17317](https://github.com/element-hq/synapse/issues/17317))
* Bump netaddr from 1.2.1 to 1.3.0. ([\#17353](https://github.com/element-hq/synapse/issues/17353))
* Bump packaging from 24.0 to 24.1. ([\#17352](https://github.com/element-hq/synapse/issues/17352))
* Bump phonenumbers from 8.13.37 to 8.13.39. ([\#17315](https://github.com/element-hq/synapse/issues/17315))
* Bump regex from 1.10.4 to 1.10.5. ([\#17290](https://github.com/element-hq/synapse/issues/17290))
* Bump requests from 2.31.0 to 2.32.2. ([\#17345](https://github.com/element-hq/synapse/issues/17345))
* Bump sentry-sdk from 2.1.1 to 2.3.1. ([\#17263](https://github.com/element-hq/synapse/issues/17263))
* Bump sentry-sdk from 2.3.1 to 2.6.0. ([\#17351](https://github.com/element-hq/synapse/issues/17351))
* Bump tornado from 6.4 to 6.4.1. ([\#17344](https://github.com/element-hq/synapse/issues/17344))
* Bump mypy from 1.8.0 to 1.9.0. ([\#17297](https://github.com/element-hq/synapse/issues/17297))
* Bump types-jsonschema from 4.21.0.20240311 to 4.22.0.20240610. ([\#17288](https://github.com/element-hq/synapse/issues/17288))
* Bump types-netaddr from 1.2.0.20240219 to 1.3.0.20240530. ([\#17314](https://github.com/element-hq/synapse/issues/17314))
* Bump types-pillow from 10.2.0.20240423 to 10.2.0.20240520. ([\#17285](https://github.com/element-hq/synapse/issues/17285))
* Bump types-pyyaml from 6.0.12.12 to 6.0.12.20240311. ([\#17316](https://github.com/element-hq/synapse/issues/17316))
* Bump typing-extensions from 4.11.0 to 4.12.2. ([\#17354](https://github.com/element-hq/synapse/issues/17354))
* Bump urllib3 from 2.0.7 to 2.2.2. ([\#17346](https://github.com/element-hq/synapse/issues/17346))

# Synapse 1.109.0 (2024-06-18)

### Internal Changes

- Fix the building of binary wheels for macOS by switching to macOS 12 CI runners. ([\#17319](https://github.com/element-hq/synapse/issues/17319))




# Synapse 1.109.0rc3 (2024-06-17)

### Bugfixes

- When rolling back to a previous Synapse version and then forwards again to this release, don't require server operators to manually run SQL. ([\#17305](https://github.com/element-hq/synapse/issues/17305), [\#17309](https://github.com/element-hq/synapse/issues/17309))

### Internal Changes

- Use the release branch for sytest in release-branch PRs. ([\#17306](https://github.com/element-hq/synapse/issues/17306))




# Synapse 1.109.0rc2 (2024-06-11)

### Bugfixes

- Fix bug where one-time-keys were not always included in `/sync` response when using workers. Introduced in v1.109.0rc1. ([\#17275](https://github.com/element-hq/synapse/issues/17275))
- Fix bug where `/sync` could get stuck due to edge case in device lists handling. Introduced in v1.109.0rc1. ([\#17292](https://github.com/element-hq/synapse/issues/17292))




# Synapse 1.109.0rc1 (2024-06-04)

### Features

- Add the ability to auto-accept invites on the behalf of users. See the [`auto_accept_invites`](https://element-hq.github.io/synapse/latest/usage/configuration/config_documentation.html#auto-accept-invites) config option for details. ([\#17147](https://github.com/element-hq/synapse/issues/17147))
- Add experimental [MSC3575](https://github.com/matrix-org/matrix-spec-proposals/pull/3575) Sliding Sync `/sync/e2ee` endpoint for to-device messages and device encryption info. ([\#17167](https://github.com/element-hq/synapse/issues/17167))
- Support [MSC3916](https://github.com/matrix-org/matrix-spec-proposals/issues/3916) by adding unstable media endpoints to `/_matrix/client`. ([\#17213](https://github.com/element-hq/synapse/issues/17213))
- Add logging to tasks managed by the task scheduler, showing CPU and database usage. ([\#17219](https://github.com/element-hq/synapse/issues/17219))

### Bugfixes

- Fix deduplicating of membership events to not create unused state groups. ([\#17164](https://github.com/element-hq/synapse/issues/17164))
- Fix bug where duplicate events could be sent down sync when using workers that are overloaded. ([\#17215](https://github.com/element-hq/synapse/issues/17215))
- Ignore attempts to send to-device messages to bad users, to avoid log spam when we try to connect to the bad server. ([\#17240](https://github.com/element-hq/synapse/issues/17240))
- Fix handling of duplicate concurrent uploading of device one-time-keys. ([\#17241](https://github.com/element-hq/synapse/issues/17241))
- Fix reporting of default tags to Sentry, such as worker name. Broke in v1.108.0. ([\#17251](https://github.com/element-hq/synapse/issues/17251))
- Fix bug where typing updates would not be sent when using workers after a restart. ([\#17252](https://github.com/element-hq/synapse/issues/17252))

### Improved Documentation

- Update the LemonLDAP documentation to say that claims should be explicitly included in the returned `id_token`, as Synapse won't request them. ([\#17204](https://github.com/element-hq/synapse/issues/17204))

### Internal Changes

- Improve DB usage when fetching related events. ([\#17083](https://github.com/element-hq/synapse/issues/17083))
- Log exceptions when failing to auto-join new user according to the `auto_join_rooms` option. ([\#17176](https://github.com/element-hq/synapse/issues/17176))
- Reduce work of calculating outbound device lists updates. ([\#17211](https://github.com/element-hq/synapse/issues/17211))
- Improve performance of calculating device lists changes in `/sync`. ([\#17216](https://github.com/element-hq/synapse/issues/17216))
- Move towards using `MultiWriterIdGenerator` everywhere. ([\#17226](https://github.com/element-hq/synapse/issues/17226))
- Replaces all usages of `StreamIdGenerator` with `MultiWriterIdGenerator`. ([\#17229](https://github.com/element-hq/synapse/issues/17229))
- Change the `allow_unsafe_locale` config option to also apply when setting up new databases. ([\#17238](https://github.com/element-hq/synapse/issues/17238))
- Fix errors in logs about closing incorrect logging contexts when media gets rejected by a module. ([\#17239](https://github.com/element-hq/synapse/issues/17239), [\#17246](https://github.com/element-hq/synapse/issues/17246))
- Clean out invalid destinations from `device_federation_outbox` table. ([\#17242](https://github.com/element-hq/synapse/issues/17242))
- Stop logging errors when receiving invalid User IDs in key querys requests. ([\#17250](https://github.com/element-hq/synapse/issues/17250))



### Updates to locked dependencies

* Bump anyhow from 1.0.83 to 1.0.86. ([\#17220](https://github.com/element-hq/synapse/issues/17220))
* Bump bcrypt from 4.1.2 to 4.1.3. ([\#17224](https://github.com/element-hq/synapse/issues/17224))
* Bump lxml from 5.2.1 to 5.2.2. ([\#17261](https://github.com/element-hq/synapse/issues/17261))
* Bump mypy-zope from 1.0.3 to 1.0.4. ([\#17262](https://github.com/element-hq/synapse/issues/17262))
* Bump phonenumbers from 8.13.35 to 8.13.37. ([\#17235](https://github.com/element-hq/synapse/issues/17235))
* Bump prometheus-client from 0.19.0 to 0.20.0. ([\#17233](https://github.com/element-hq/synapse/issues/17233))
* Bump pyasn1 from 0.5.1 to 0.6.0. ([\#17223](https://github.com/element-hq/synapse/issues/17223))
* Bump pyicu from 2.13 to 2.13.1. ([\#17236](https://github.com/element-hq/synapse/issues/17236))
* Bump pyopenssl from 24.0.0 to 24.1.0. ([\#17234](https://github.com/element-hq/synapse/issues/17234))
* Bump serde from 1.0.201 to 1.0.202. ([\#17221](https://github.com/element-hq/synapse/issues/17221))
* Bump serde from 1.0.202 to 1.0.203. ([\#17232](https://github.com/element-hq/synapse/issues/17232))
* Bump twine from 5.0.0 to 5.1.0. ([\#17225](https://github.com/element-hq/synapse/issues/17225))
* Bump types-psycopg2 from 2.9.21.20240311 to 2.9.21.20240417. ([\#17222](https://github.com/element-hq/synapse/issues/17222))
* Bump types-pyopenssl from 24.0.0.20240311 to 24.1.0.20240425. ([\#17260](https://github.com/element-hq/synapse/issues/17260))

# Synapse 1.108.0 (2024-05-28)

No significant changes since 1.108.0rc1.




# Synapse 1.108.0rc1 (2024-05-21)

### Features

- Add a feature that allows clients to query the configured federation whitelist. Disabled by default. ([\#16848](https://github.com/element-hq/synapse/issues/16848), [\#17199](https://github.com/element-hq/synapse/issues/17199))
- Add the ability to allow numeric user IDs with a specific prefix when in the CAS flow. Contributed by Aurélien Grimpard. ([\#17098](https://github.com/element-hq/synapse/issues/17098))

### Bugfixes

- Fix bug where push rules would be empty in `/sync` for some accounts. Introduced in v1.93.0. ([\#17142](https://github.com/element-hq/synapse/issues/17142))
- Add support for optional whitespace around the Federation API's `Authorization` header's parameter commas. ([\#17145](https://github.com/element-hq/synapse/issues/17145))
- Fix bug where disabling room publication prevented public rooms being created on workers. ([\#17177](https://github.com/element-hq/synapse/issues/17177), [\#17184](https://github.com/element-hq/synapse/issues/17184))

### Improved Documentation

- Document [`/v1/make_knock`](https://spec.matrix.org/v1.10/server-server-api/#get_matrixfederationv1make_knockroomiduserid) and [`/v1/send_knock/`](https://spec.matrix.org/v1.10/server-server-api/#put_matrixfederationv1send_knockroomideventid) federation endpoints as worker-compatible. ([\#17058](https://github.com/element-hq/synapse/issues/17058))
- Update User Admin API with note about prefixing OIDC external_id providers. ([\#17139](https://github.com/element-hq/synapse/issues/17139))
- Clarify the state of the created room when using the `autocreate_auto_join_room_preset` config option. ([\#17150](https://github.com/element-hq/synapse/issues/17150))
- Update the Admin FAQ with the current libjemalloc version for latest Debian stable. Additionally update the name of the "push_rules" stream in the Workers documentation. ([\#17171](https://github.com/element-hq/synapse/issues/17171))

### Internal Changes

- Add note to reflect that [MSC3886](https://github.com/matrix-org/matrix-spec-proposals/pull/3886) is closed but will remain supported for some time. ([\#17151](https://github.com/element-hq/synapse/issues/17151))
- Update dependency PyO3 to 0.21. ([\#17162](https://github.com/element-hq/synapse/issues/17162))
- Fixes linter errors found in PR #17147. ([\#17166](https://github.com/element-hq/synapse/issues/17166))
- Bump black from 24.2.0 to 24.4.2. ([\#17170](https://github.com/element-hq/synapse/issues/17170))
- Cache literal sync filter validation for performance. ([\#17186](https://github.com/element-hq/synapse/issues/17186))
- Improve performance by fixing a reactor pause. ([\#17192](https://github.com/element-hq/synapse/issues/17192))
- Route `/make_knock` and `/send_knock` federation APIs to the federation reader worker in Complement test runs. ([\#17195](https://github.com/element-hq/synapse/issues/17195))
- Prepare sync handler to be able to return different sync responses (`SyncVersion`). ([\#17200](https://github.com/element-hq/synapse/issues/17200))
- Organize the sync cache key parameter outside of the sync config (separate concerns). ([\#17201](https://github.com/element-hq/synapse/issues/17201))
- Refactor `SyncResultBuilder` assembly to its own function. ([\#17202](https://github.com/element-hq/synapse/issues/17202))
- Rename to be obvious: `joined_rooms` -> `joined_room_ids`. ([\#17203](https://github.com/element-hq/synapse/issues/17203), [\#17208](https://github.com/element-hq/synapse/issues/17208))
- Add a short pause when rate-limiting a request. ([\#17210](https://github.com/element-hq/synapse/issues/17210))



### Updates to locked dependencies

* Bump cryptography from 42.0.5 to 42.0.7. ([\#17180](https://github.com/element-hq/synapse/issues/17180))
* Bump gitpython from 3.1.41 to 3.1.43. ([\#17181](https://github.com/element-hq/synapse/issues/17181))
* Bump immutabledict from 4.1.0 to 4.2.0. ([\#17179](https://github.com/element-hq/synapse/issues/17179))
* Bump sentry-sdk from 1.40.3 to 2.1.1. ([\#17178](https://github.com/element-hq/synapse/issues/17178))
* Bump serde from 1.0.200 to 1.0.201. ([\#17183](https://github.com/element-hq/synapse/issues/17183))
* Bump serde_json from 1.0.116 to 1.0.117. ([\#17182](https://github.com/element-hq/synapse/issues/17182))

Synapse 1.107.0 (2024-05-14)
============================

No significant changes since 1.107.0rc1.


# Synapse 1.107.0rc1 (2024-05-07)

### Features

- Add preliminary support for [MSC3823: Account Suspension](https://github.com/matrix-org/matrix-spec-proposals/pull/3823). ([\#17051](https://github.com/element-hq/synapse/issues/17051))
- Declare support for [Matrix v1.10](https://matrix.org/blog/2024/03/22/matrix-v1.10-release/). Contributed by @clokep. ([\#17082](https://github.com/element-hq/synapse/issues/17082))
- Add support for [MSC4115: membership metadata on events](https://github.com/matrix-org/matrix-spec-proposals/pull/4115). ([\#17104](https://github.com/element-hq/synapse/issues/17104), [\#17137](https://github.com/element-hq/synapse/issues/17137))

### Bugfixes

- Fixed search feature of Element Android on homesevers using SQLite by returning search terms as search highlights. ([\#17000](https://github.com/element-hq/synapse/issues/17000))
- Fixes a bug introduced in v1.52.0 where the `destination` query parameter for the  [Destination Rooms Admin API](https://element-hq.github.io/synapse/v1.105/usage/administration/admin_api/federation.html#destination-rooms) failed to actually filter returned rooms. ([\#17077](https://github.com/element-hq/synapse/issues/17077))
- For MSC3266 room summaries, support queries at the recommended endpoint of `/_matrix/client/unstable/im.nheko.summary/summary/{roomIdOrAlias}`. The existing endpoint of `/_matrix/client/unstable/im.nheko.summary/rooms/{roomIdOrAlias}/summary` is deprecated. ([\#17078](https://github.com/element-hq/synapse/issues/17078))
- Apply user email & picture during OIDC registration if present & selected. ([\#17120](https://github.com/element-hq/synapse/issues/17120))
- Improve error message for cross signing reset with [MSC3861](https://github.com/matrix-org/matrix-spec-proposals/pull/3861) enabled. ([\#17121](https://github.com/element-hq/synapse/issues/17121))
- Fix a bug which meant that to-device messages received over federation could be dropped when the server was under load or networking problems caused problems between Synapse processes or the database. ([\#17127](https://github.com/element-hq/synapse/issues/17127))
- Fix bug where `StreamChangeCache` would not respect configured cache factors. ([\#17152](https://github.com/element-hq/synapse/issues/17152))

### Updates to the Docker image

- Correct licensing metadata on Docker image. ([\#17141](https://github.com/element-hq/synapse/issues/17141))

### Improved Documentation

- Update the `event_cache_size` and `global_factor` configuration options' documentation. ([\#17071](https://github.com/element-hq/synapse/issues/17071))
- Remove broken sphinx docs. ([\#17073](https://github.com/element-hq/synapse/issues/17073), [\#17148](https://github.com/element-hq/synapse/issues/17148))
- Add RuntimeDirectory to example matrix-synapse.service systemd unit. ([\#17084](https://github.com/element-hq/synapse/issues/17084))
- Fix various small typos throughout the docs. ([\#17114](https://github.com/element-hq/synapse/issues/17114))
- Update enable_notifs configuration documentation. ([\#17116](https://github.com/element-hq/synapse/issues/17116))
- Update the Upgrade Notes with the latest minimum supported Rust version of 1.66.0. Contributed by @jahway603. ([\#17140](https://github.com/element-hq/synapse/issues/17140))

### Internal Changes

- Enable [MSC3266](https://github.com/matrix-org/matrix-spec-proposals/pull/3266) by default in the Synapse Complement image. ([\#17105](https://github.com/element-hq/synapse/issues/17105))
- Add optimisation to `StreamChangeCache.get_entities_changed(..)`. ([\#17130](https://github.com/element-hq/synapse/issues/17130))



### Updates to locked dependencies

* Bump furo from 2024.1.29 to 2024.4.27. ([\#17133](https://github.com/element-hq/synapse/issues/17133))
* Bump idna from 3.6 to 3.7. ([\#17136](https://github.com/element-hq/synapse/issues/17136))
* Bump jsonschema from 4.21.1 to 4.22.0. ([\#17157](https://github.com/element-hq/synapse/issues/17157))
* Bump lxml from 5.1.0 to 5.2.1. ([\#17158](https://github.com/element-hq/synapse/issues/17158))
* Bump phonenumbers from 8.13.29 to 8.13.35. ([\#17106](https://github.com/element-hq/synapse/issues/17106))
- Bump pillow from 10.2.0 to 10.3.0. ([\#17146](https://github.com/element-hq/synapse/issues/17146))
* Bump pydantic from 2.6.4 to 2.7.0. ([\#17107](https://github.com/element-hq/synapse/issues/17107))
* Bump pydantic from 2.7.0 to 2.7.1. ([\#17160](https://github.com/element-hq/synapse/issues/17160))
* Bump pyicu from 2.12 to 2.13. ([\#17109](https://github.com/element-hq/synapse/issues/17109))
* Bump serde from 1.0.197 to 1.0.198. ([\#17111](https://github.com/element-hq/synapse/issues/17111))
* Bump serde from 1.0.198 to 1.0.199. ([\#17132](https://github.com/element-hq/synapse/issues/17132))
* Bump serde from 1.0.199 to 1.0.200. ([\#17161](https://github.com/element-hq/synapse/issues/17161))
* Bump serde_json from 1.0.115 to 1.0.116. ([\#17112](https://github.com/element-hq/synapse/issues/17112))
- Update `tornado` Python dependency from 6.2 to 6.4. ([\#17131](https://github.com/element-hq/synapse/issues/17131))
* Bump twisted from 23.10.0 to 24.3.0. ([\#17135](https://github.com/element-hq/synapse/issues/17135))
* Bump types-bleach from 6.1.0.1 to 6.1.0.20240331. ([\#17110](https://github.com/element-hq/synapse/issues/17110))
* Bump types-pillow from 10.2.0.20240415 to 10.2.0.20240423. ([\#17159](https://github.com/element-hq/synapse/issues/17159))
* Bump types-setuptools from 69.0.0.20240125 to 69.5.0.20240423. ([\#17134](https://github.com/element-hq/synapse/issues/17134))

# Synapse 1.106.0 (2024-04-30)

No significant changes since 1.106.0rc1.




# Synapse 1.106.0rc1 (2024-04-25)

### Features

- Send an email if the address is already bound to an user account. ([\#16819](https://github.com/element-hq/synapse/issues/16819))
- Implement the rendezvous mechanism described by [MSC4108](https://github.com/matrix-org/matrix-spec-proposals/issues/4108). ([\#17056](https://github.com/element-hq/synapse/issues/17056))
- Support delegating the rendezvous mechanism described [MSC4108](https://github.com/matrix-org/matrix-spec-proposals/issues/4108) to an external implementation. ([\#17086](https://github.com/element-hq/synapse/issues/17086))

### Bugfixes

- Add validation to ensure that the `limit` parameter on `/publicRooms` is non-negative. ([\#16920](https://github.com/element-hq/synapse/issues/16920))
- Return `400 M_NOT_JSON` upon receiving invalid JSON in query parameters across various client and admin endpoints, rather than an internal server error. ([\#16923](https://github.com/element-hq/synapse/issues/16923))
- Make the CSAPI endpoint `/keys/device_signing/upload` idempotent. ([\#16943](https://github.com/element-hq/synapse/issues/16943))
- Redact membership events if the user requested erasure upon deactivating. ([\#17076](https://github.com/element-hq/synapse/issues/17076))

### Improved Documentation

- Add a prompt in the contributing guide to manually configure icu4c. ([\#17069](https://github.com/element-hq/synapse/issues/17069))
- Clarify what part of message retention is still experimental. ([\#17099](https://github.com/element-hq/synapse/issues/17099))

### Internal Changes

- Use new receipts column to optimise receipt and push action SQL queries. Contributed by Nick @ Beeper (@fizzadar). ([\#17032](https://github.com/element-hq/synapse/issues/17032), [\#17096](https://github.com/element-hq/synapse/issues/17096))
- Fix mypy with latest Twisted release. ([\#17036](https://github.com/element-hq/synapse/issues/17036))
- Bump minimum supported Rust version to 1.66.0. ([\#17079](https://github.com/element-hq/synapse/issues/17079))
- Add helpers to transform Twisted requests to Rust http Requests/Responses. ([\#17081](https://github.com/element-hq/synapse/issues/17081))
- Fix type annotation for `visited_chains` after `mypy` upgrade. ([\#17125](https://github.com/element-hq/synapse/issues/17125))



### Updates to locked dependencies

* Bump anyhow from 1.0.81 to 1.0.82. ([\#17095](https://github.com/element-hq/synapse/issues/17095))
* Bump peaceiris/actions-gh-pages from 3.9.3 to 4.0.0. ([\#17087](https://github.com/element-hq/synapse/issues/17087))
* Bump peaceiris/actions-mdbook from 1.2.0 to 2.0.0. ([\#17089](https://github.com/element-hq/synapse/issues/17089))
* Bump pyasn1-modules from 0.3.0 to 0.4.0. ([\#17093](https://github.com/element-hq/synapse/issues/17093))
* Bump pygithub from 2.2.0 to 2.3.0. ([\#17092](https://github.com/element-hq/synapse/issues/17092))
* Bump ruff from 0.3.5 to 0.3.7. ([\#17094](https://github.com/element-hq/synapse/issues/17094))
* Bump sigstore/cosign-installer from 3.4.0 to 3.5.0. ([\#17088](https://github.com/element-hq/synapse/issues/17088))
* Bump twine from 4.0.2 to 5.0.0. ([\#17091](https://github.com/element-hq/synapse/issues/17091))
* Bump types-pillow from 10.2.0.20240406 to 10.2.0.20240415. ([\#17090](https://github.com/element-hq/synapse/issues/17090))

# Synapse 1.105.1 (2024-04-23)

## Security advisory

The following issues are fixed in 1.105.1.

- [GHSA-3h7q-rfh9-xm4v](https://github.com/element-hq/synapse/security/advisories/GHSA-3h7q-rfh9-xm4v) / [CVE-2024-31208](https://cve.mitre.org/cgi-bin/cvename.cgi?name=CVE-2024-31208) — High Severity

  Weakness in auth chain indexing allows DoS from remote room members through disk fill and high CPU usage.

See the advisories for more details. If you have any questions, email security@element.io.



# Synapse 1.105.0 (2024-04-16)

No significant changes since 1.105.0rc1.




# Synapse 1.105.0rc1 (2024-04-11)

### Features

- Stabilize support for [MSC4010](https://github.com/matrix-org/matrix-spec-proposals/pull/4010) which clarifies the interaction of push rules and account data. Contributed by @clokep. ([\#17022](https://github.com/element-hq/synapse/issues/17022))
- Stabilize support for [MSC3981](https://github.com/matrix-org/matrix-spec-proposals/pull/3981): `/relations` recursion. Contributed by @clokep. ([\#17023](https://github.com/element-hq/synapse/issues/17023))
- Add support for moving `/pushrules` off of main process. ([\#17037](https://github.com/element-hq/synapse/issues/17037), [\#17038](https://github.com/element-hq/synapse/issues/17038))

### Bugfixes

- Fix various long-standing bugs which could cause incorrect state to be returned from `/sync` in certain situations. ([\#16930](https://github.com/element-hq/synapse/issues/16930), [\#16932](https://github.com/element-hq/synapse/issues/16932), [\#16942](https://github.com/element-hq/synapse/issues/16942), [\#17064](https://github.com/element-hq/synapse/issues/17064), [\#17065](https://github.com/element-hq/synapse/issues/17065), [\#17066](https://github.com/element-hq/synapse/issues/17066))
- Fix server notice rooms not always being created as unencrypted rooms, even when `encryption_enabled_by_default_for_room_type` is in use (server notices are always unencrypted). ([\#17033](https://github.com/element-hq/synapse/issues/17033))
- Fix the `.m.rule.encrypted_room_one_to_one` and `.m.rule.room_one_to_one` default underride push rules being in the wrong order. Contributed by @Sumpy1. ([\#17043](https://github.com/element-hq/synapse/issues/17043))

### Internal Changes

- Refactor auth chain fetching to reduce duplication. ([\#17044](https://github.com/element-hq/synapse/issues/17044))
- Improve database performance by adding a missing index to `access_tokens.refresh_token_id`. ([\#17045](https://github.com/element-hq/synapse/issues/17045), [\#17054](https://github.com/element-hq/synapse/issues/17054))
- Improve database performance by reducing number of receipts fetched when sending push notifications. ([\#17049](https://github.com/element-hq/synapse/issues/17049))



### Updates to locked dependencies

* Bump packaging from 23.2 to 24.0. ([\#17027](https://github.com/element-hq/synapse/issues/17027))
* Bump regex from 1.10.3 to 1.10.4. ([\#17028](https://github.com/element-hq/synapse/issues/17028))
* Bump ruff from 0.3.2 to 0.3.5. ([\#17060](https://github.com/element-hq/synapse/issues/17060))
* Bump serde_json from 1.0.114 to 1.0.115. ([\#17041](https://github.com/element-hq/synapse/issues/17041))
* Bump types-pillow from 10.2.0.20240125 to 10.2.0.20240406. ([\#17061](https://github.com/element-hq/synapse/issues/17061))
* Bump types-requests from 2.31.0.20240125 to 2.31.0.20240406. ([\#17063](https://github.com/element-hq/synapse/issues/17063))
* Bump typing-extensions from 4.9.0 to 4.11.0. ([\#17062](https://github.com/element-hq/synapse/issues/17062))

# Synapse 1.104.0 (2024-04-02)

### Bugfixes

- Fix regression when using OIDC provider. Introduced in v1.104.0rc1. ([\#17031](https://github.com/element-hq/synapse/issues/17031))


# Synapse 1.104.0rc1 (2024-03-26)

### Features

- Add an OIDC config to specify extra parameters for the authorization grant URL. IT can be useful to pass an ACR value for example. ([\#16971](https://github.com/element-hq/synapse/issues/16971))
- Add support for OIDC provider returning JWT. ([\#16972](https://github.com/element-hq/synapse/issues/16972), [\#17031](https://github.com/element-hq/synapse/issues/17031))

### Bugfixes

- Fix a bug which meant that, under certain circumstances, we might never retry sending events or to-device messages over federation after a failure. ([\#16925](https://github.com/element-hq/synapse/issues/16925))
- Fix various long-standing bugs which could cause incorrect state to be returned from `/sync` in certain situations. ([\#16949](https://github.com/element-hq/synapse/issues/16949))
- Fix case in which `m.fully_read` marker would not get updated. Contributed by @SpiritCroc. ([\#16990](https://github.com/element-hq/synapse/issues/16990))
- Fix bug which did not retract a user's pending knocks at rooms when their account was deactivated. Contributed by @hanadi92. ([\#17010](https://github.com/element-hq/synapse/issues/17010))

### Updates to the Docker image

- Updated `start.py` to generate config using the correct user ID when running as root (fixes [\#16824](https://github.com/element-hq/synapse/issues/16824), [\#15202](https://github.com/element-hq/synapse/issues/15202)). ([\#16978](https://github.com/element-hq/synapse/issues/16978))

### Improved Documentation

- Add a query to force a refresh of a remote user's device list to the "Useful SQL for Admins" documentation page. ([\#16892](https://github.com/element-hq/synapse/issues/16892))
- Minor grammatical corrections to the upgrade documentation. ([\#16965](https://github.com/element-hq/synapse/issues/16965))
- Fix the sort order for the documentation version picker, so that newer releases appear above older ones. ([\#16966](https://github.com/element-hq/synapse/issues/16966))
- Remove recommendation for a specific poetry version from contributing guide. ([\#17002](https://github.com/element-hq/synapse/issues/17002))

### Internal Changes

- Improve lock performance when a lot of locks are all waiting for a single lock to be released. ([\#16840](https://github.com/element-hq/synapse/issues/16840))
- Update power level default for public rooms. ([\#16907](https://github.com/element-hq/synapse/issues/16907))
- Improve event validation. ([\#16908](https://github.com/element-hq/synapse/issues/16908))
- Multi-worker-docker-container: disable log buffering. ([\#16919](https://github.com/element-hq/synapse/issues/16919))
- Refactor state delta calculation in `/sync` handler. ([\#16929](https://github.com/element-hq/synapse/issues/16929))
- Clarify docs for some room state functions. ([\#16950](https://github.com/element-hq/synapse/issues/16950))
- Specify IP subnets in canonical form. ([\#16953](https://github.com/element-hq/synapse/issues/16953))
- As done for SAML mapping provider, let's pass the module API to the OIDC one so the mapper can do more logic in its code. ([\#16974](https://github.com/element-hq/synapse/issues/16974))
- Allow containers building on top of Synapse's Complement container is use the included PostgreSQL cluster. ([\#16985](https://github.com/element-hq/synapse/issues/16985))
- Raise poetry-core version cap to 1.9.0. ([\#16986](https://github.com/element-hq/synapse/issues/16986))
- Patch the db conn pool sooner in tests. ([\#17017](https://github.com/element-hq/synapse/issues/17017))



### Updates to locked dependencies

* Bump anyhow from 1.0.80 to 1.0.81. ([\#17009](https://github.com/element-hq/synapse/issues/17009))
* Bump black from 23.10.1 to 24.2.0. ([\#16936](https://github.com/element-hq/synapse/issues/16936))
* Bump cryptography from 41.0.7 to 42.0.5. ([\#16958](https://github.com/element-hq/synapse/issues/16958))
* Bump dawidd6/action-download-artifact from 3.1.1 to 3.1.2. ([\#16960](https://github.com/element-hq/synapse/issues/16960))
* Bump dawidd6/action-download-artifact from 3.1.2 to 3.1.4. ([\#17008](https://github.com/element-hq/synapse/issues/17008))
* Bump jinja2 from 3.1.2 to 3.1.3. ([\#17005](https://github.com/element-hq/synapse/issues/17005))
* Bump log from 0.4.20 to 0.4.21. ([\#16977](https://github.com/element-hq/synapse/issues/16977))
* Bump mypy from 1.5.1 to 1.8.0. ([\#16901](https://github.com/element-hq/synapse/issues/16901))
* Bump netaddr from 0.9.0 to 1.2.1. ([\#17006](https://github.com/element-hq/synapse/issues/17006))
* Bump pydantic from 2.6.0 to 2.6.4. ([\#17004](https://github.com/element-hq/synapse/issues/17004))
* Bump pyo3 from 0.20.2 to 0.20.3. ([\#16962](https://github.com/element-hq/synapse/issues/16962))
* Bump ruff from 0.1.14 to 0.3.2. ([\#16994](https://github.com/element-hq/synapse/issues/16994))
* Bump serde from 1.0.196 to 1.0.197. ([\#16963](https://github.com/element-hq/synapse/issues/16963))
* Bump serde_json from 1.0.113 to 1.0.114. ([\#16961](https://github.com/element-hq/synapse/issues/16961))
* Bump types-jsonschema from 4.21.0.20240118 to 4.21.0.20240311. ([\#17007](https://github.com/element-hq/synapse/issues/17007))
* Bump types-psycopg2 from 2.9.21.16 to 2.9.21.20240311. ([\#16995](https://github.com/element-hq/synapse/issues/16995))
* Bump types-pyopenssl from 23.3.0.0 to 24.0.0.20240311. ([\#17003](https://github.com/element-hq/synapse/issues/17003))

# Synapse 1.103.0 (2024-03-19)

No significant changes since 1.103.0rc1.




# Synapse 1.103.0rc1 (2024-03-12)

### Features

- Add a new [List Accounts v3](https://element-hq.github.io/synapse/v1.103/admin_api/user_admin_api.html#list-accounts-v3) Admin API with improved deactivated user filtering capabilities. ([\#16874](https://github.com/element-hq/synapse/issues/16874))
- Include `Retry-After` header by default per [MSC4041](https://github.com/matrix-org/matrix-spec-proposals/pull/4041). Contributed by @clokep. ([\#16947](https://github.com/element-hq/synapse/issues/16947))

### Bugfixes

- Fix joining remote rooms when a module uses the `on_new_event` callback. This callback may now pass partial state events instead of the full state for remote rooms. Introduced in v1.76.0. ([\#16973](https://github.com/element-hq/synapse/issues/16973))
- Fix performance issue when joining very large rooms that can cause the server to lock up. Introduced in v1.100.0. Contributed by @ggogel. ([\#16968](https://github.com/element-hq/synapse/issues/16968))

### Improved Documentation

- Add HAProxy example for single port operation to reverse proxy documentation. Contributed by Georg Pfuetzenreuter (@tacerus). ([\#16768](https://github.com/element-hq/synapse/issues/16768))
- Improve the documentation around running Complement tests with new configuration parameters. ([\#16946](https://github.com/element-hq/synapse/issues/16946))
- Add docs on upgrading from a very old version. ([\#16951](https://github.com/element-hq/synapse/issues/16951))


### Updates to locked dependencies

* Bump JasonEtco/create-an-issue from 2.9.1 to 2.9.2. ([\#16934](https://github.com/element-hq/synapse/issues/16934))
* Bump anyhow from 1.0.79 to 1.0.80. ([\#16935](https://github.com/element-hq/synapse/issues/16935))
* Bump dawidd6/action-download-artifact from 3.0.0 to 3.1.1. ([\#16933](https://github.com/element-hq/synapse/issues/16933))
* Bump furo from 2023.9.10 to 2024.1.29. ([\#16939](https://github.com/element-hq/synapse/issues/16939))
* Bump pyopenssl from 23.3.0 to 24.0.0. ([\#16937](https://github.com/element-hq/synapse/issues/16937))
* Bump types-netaddr from 0.10.0.20240106 to 1.2.0.20240219. ([\#16938](https://github.com/element-hq/synapse/issues/16938))


# Synapse 1.102.0 (2024-03-05)

### Bugfixes

- Revert https://github.com/element-hq/synapse/pull/16756, which caused incorrect notification counts on mobile clients since v1.100.0. ([\#16979](https://github.com/element-hq/synapse/issues/16979))


# Synapse 1.102.0rc1 (2024-02-20)

### Features

- A metric was added for emails sent by Synapse, broken down by type: `synapse_emails_sent_total`. Contributed by Remi Rampin. ([\#16881](https://github.com/element-hq/synapse/issues/16881))

### Bugfixes

- Do not send multiple concurrent requests for keys for the same server. ([\#16894](https://github.com/element-hq/synapse/issues/16894))
- Fix performance issue when joining very large rooms that can cause the server to lock up. Introduced in v1.100.0. ([\#16903](https://github.com/element-hq/synapse/issues/16903))
- Always prefer unthreaded receipt when >1 exist ([MSC4102](https://github.com/matrix-org/matrix-spec-proposals/pull/4102)). ([\#16927](https://github.com/element-hq/synapse/issues/16927))

### Improved Documentation

- Fix a small typo in the Rooms section of the Admin API documentation. Contributed by @RainerZufall187. ([\#16857](https://github.com/element-hq/synapse/issues/16857))

### Internal Changes

- Don't invalidate the entire event cache when we purge history. ([\#16905](https://github.com/element-hq/synapse/issues/16905))
- Add experimental config option to not send device list updates for specific users. ([\#16909](https://github.com/element-hq/synapse/issues/16909))
- Fix incorrect docker hub link in release script. ([\#16910](https://github.com/element-hq/synapse/issues/16910))



### Updates to locked dependencies

* Bump attrs from 23.1.0 to 23.2.0. ([\#16899](https://github.com/element-hq/synapse/issues/16899))
* Bump bcrypt from 4.0.1 to 4.1.2. ([\#16900](https://github.com/element-hq/synapse/issues/16900))
* Bump pygithub from 2.1.1 to 2.2.0. ([\#16902](https://github.com/element-hq/synapse/issues/16902))
* Bump sentry-sdk from 1.40.0 to 1.40.3. ([\#16898](https://github.com/element-hq/synapse/issues/16898))

# Synapse 1.101.0 (2024-02-13)

### Bugfixes

- Fix performance regression when fetching auth chains from the DB. Introduced in v1.100.0. ([\#16893](https://github.com/element-hq/synapse/issues/16893))




# Synapse 1.101.0rc1 (2024-02-06)

### Improved Documentation

- Fix broken links in the documentation. ([\#16853](https://github.com/element-hq/synapse/issues/16853))
- Update MacOS installation instructions to mention that libicu is optional. ([\#16854](https://github.com/element-hq/synapse/issues/16854))
- The version picker now correctly lists versions after `v1.98.0`. ([\#16880](https://github.com/element-hq/synapse/issues/16880))

### Internal Changes

- Add support for stabilised [MSC3981](https://github.com/matrix-org/matrix-spec-proposals/pull/3981) that adds a `recurse` parameter on the `/relations` API. ([\#16842](https://github.com/element-hq/synapse/issues/16842))



### Updates to locked dependencies

* Bump dorny/paths-filter from 2 to 3. ([\#16869](https://github.com/element-hq/synapse/issues/16869))
* Bump gitpython from 3.1.40 to 3.1.41. ([\#16850](https://github.com/element-hq/synapse/issues/16850))
* Bump hiredis from 2.2.3 to 2.3.2. ([\#16862](https://github.com/element-hq/synapse/issues/16862))
* Bump jsonschema from 4.20.0 to 4.21.1. ([\#16887](https://github.com/element-hq/synapse/issues/16887))
* Bump lxml-stubs from 0.4.0 to 0.5.1. ([\#16885](https://github.com/element-hq/synapse/issues/16885))
* Bump mypy-zope from 1.0.1 to 1.0.3. ([\#16865](https://github.com/element-hq/synapse/issues/16865))
* Bump phonenumbers from 8.13.26 to 8.13.29. ([\#16868](https://github.com/element-hq/synapse/issues/16868))
* Bump pydantic from 2.5.3 to 2.6.0. ([\#16888](https://github.com/element-hq/synapse/issues/16888))
* Bump sentry-sdk from 1.39.1 to 1.40.0. ([\#16889](https://github.com/element-hq/synapse/issues/16889))
* Bump serde from 1.0.195 to 1.0.196. ([\#16867](https://github.com/element-hq/synapse/issues/16867))
* Bump serde_json from 1.0.111 to 1.0.113. ([\#16866](https://github.com/element-hq/synapse/issues/16866))
* Bump sigstore/cosign-installer from 3.3.0 to 3.4.0. ([\#16890](https://github.com/element-hq/synapse/issues/16890))
* Bump types-pillow from 10.1.0.2 to 10.2.0.20240125. ([\#16864](https://github.com/element-hq/synapse/issues/16864))
* Bump types-requests from 2.31.0.10 to 2.31.0.20240125. ([\#16886](https://github.com/element-hq/synapse/issues/16886))
* Bump types-setuptools from 69.0.0.0 to 69.0.0.20240125. ([\#16863](https://github.com/element-hq/synapse/issues/16863))

# Synapse 1.100.0 (2024-01-30)

No significant changes since 1.100.0rc3.




# Synapse 1.100.0rc3 (2024-01-24)

### Bugfixes

- Fix database performance regression due to changing Postgres table statistics. Introduced in v1.100.0rc1. ([\#16849](https://github.com/element-hq/synapse/issues/16849))




# Synapse 1.100.0rc2 (2024-01-24)

This version is the same as 1.100.0rc1 but with fixes to the release process.

### Internal Changes

- Downgrade the `download-artifact` and `upload-artifact` actions to v3 due to breaking changes. ([\#16847](https://github.com/element-hq/synapse/issues/16847))


# Synapse 1.100.0rc1 (2024-01-23)

*This version was never released to PyPI or the Debian repository due to failures in the automatic part of the release process.*

### Features

- Advertise experimental support for [MSC4028](https://github.com/matrix-org/matrix-spec-proposals/pull/4028) through `/_matrix/clients/versions` if enabled. Contributed by @hanadi92. ([\#16787](https://github.com/element-hq/synapse/issues/16787))

### Bugfixes

- Handle wildcard type filters properly for room messages endpoint. Contributed by Mo Balaa. ([\#14984](https://github.com/element-hq/synapse/issues/14984))

### Improved Documentation

- Add a link to the "Request log format" explainer on the "Logging sample config" documentation page. ([\#16778](https://github.com/element-hq/synapse/issues/16778))
- Fix broken links in issue templates and documentation. ([\#16810](https://github.com/element-hq/synapse/issues/16810))
- NGINX listen http2 deprecation in documentation template for reverse proxy. ([\#16831](https://github.com/element-hq/synapse/issues/16831))

### Internal Changes

- Faster partial join to room with complex auth graph. ([\#7](https://github.com/element-hq/synapse/issues/7))
- Improve DB performance of calculating badge counts for push. ([\#16756](https://github.com/element-hq/synapse/issues/16756))
- Split up deleting devices into batches. ([\#16766](https://github.com/element-hq/synapse/issues/16766))
- Remove CI check for sign-off as we require a CLA signature instead. ([\#16776](https://github.com/element-hq/synapse/issues/16776))
- Ensure CI fails when linting fails to make sure auto-merge does the correct thing. ([\#16781](https://github.com/element-hq/synapse/issues/16781))
- Faster load recents for sync by reducing amount of state pulled out. ([\#16783](https://github.com/element-hq/synapse/issues/16783))
- Reduce amount of state pulled out when querying federation hierachy. ([\#16785](https://github.com/element-hq/synapse/issues/16785))
- Pull less state out of the DB when we retry fetching old events during backfill. ([\#16788](https://github.com/element-hq/synapse/issues/16788))
- Optimize query for fetching to-device messages in `/sync`. ([\#16805](https://github.com/element-hq/synapse/issues/16805))
- Reject OIDC config when `client_secret` isn't specified, but the auth method requires one. ([\#16806](https://github.com/element-hq/synapse/issues/16806))
- Allow room creation but not publishing to continue if room publication rules are violated when creating
  a new room. ([\#16811](https://github.com/element-hq/synapse/issues/16811))
- Bump minimum supported Rust version to 1.65.0. ([\#16818](https://github.com/element-hq/synapse/issues/16818))
- Fixup copyright lines in file headers after the licensing change. ([\#16820](https://github.com/element-hq/synapse/issues/16820))
- Add a `--generate-only` option to the internal configuration/launch script for Complement. ([\#16828](https://github.com/element-hq/synapse/issues/16828))
- Preparatory work for tweaking performance of auth chain lookups. ([\#16833](https://github.com/element-hq/synapse/issues/16833))
- Speed up e2e device keys queries for bot accounts. ([\#16841](https://github.com/element-hq/synapse/issues/16841))

### Updates to locked dependencies

* Bump actions/cache from 3 to 4. ([\#16832](https://github.com/element-hq/synapse/issues/16832))
* Bump actions/download-artifact from 3 to 4. ([\#16795](https://github.com/element-hq/synapse/issues/16795))
* Bump actions/upload-artifact from 3 to 4. ([\#16796](https://github.com/element-hq/synapse/issues/16796))
* Bump anyhow from 1.0.75 to 1.0.79. ([\#16789](https://github.com/element-hq/synapse/issues/16789))
* Bump authlib from 1.2.1 to 1.3.0. ([\#16801](https://github.com/element-hq/synapse/issues/16801))
* Bump dawidd6/action-download-artifact from 2.28.0 to 3.0.0. ([\#16794](https://github.com/element-hq/synapse/issues/16794))
* Bump immutabledict from 4.0.0 to 4.1.0. ([\#16812](https://github.com/element-hq/synapse/issues/16812))
* Bump isort from 5.13.1 to 5.13.2. ([\#16835](https://github.com/element-hq/synapse/issues/16835))
* Bump lxml from 4.9.3 to 5.1.0. ([\#16813](https://github.com/element-hq/synapse/issues/16813))
* Bump pillow from 10.1.0 to 10.2.0. ([\#16802](https://github.com/element-hq/synapse/issues/16802))
* Bump pydantic from 2.5.2 to 2.5.3. ([\#16836](https://github.com/element-hq/synapse/issues/16836))
* Bump pyo3 from 0.20.0 to 0.20.2. ([\#16791](https://github.com/element-hq/synapse/issues/16791))
* Bump regex from 1.9.6 to 1.10.3. ([\#16837](https://github.com/element-hq/synapse/issues/16837))
* Bump ruff from 0.1.13 to 0.1.14. ([\#16838](https://github.com/element-hq/synapse/issues/16838))
* Bump ruff from 0.1.7 to 0.1.13. ([\#16814](https://github.com/element-hq/synapse/issues/16814))
* Bump sentry-sdk from 1.35.0 to 1.39.1. ([\#16799](https://github.com/element-hq/synapse/issues/16799))
* Bump serde_json from 1.0.108 to 1.0.111. ([\#16792](https://github.com/element-hq/synapse/issues/16792))
* Bump service-identity from 23.1.0 to 24.1.0. ([\#16816](https://github.com/element-hq/synapse/issues/16816))
* Bump types-commonmark from 0.9.2.4 to 0.9.2.20240106. ([\#16797](https://github.com/element-hq/synapse/issues/16797))
* Bump types-jsonschema from 4.20.0.0 to 4.20.0.20240105. ([\#16800](https://github.com/element-hq/synapse/issues/16800))
* Bump types-jsonschema from 4.20.0.20240105 to 4.21.0.20240118. ([\#16834](https://github.com/element-hq/synapse/issues/16834))
* Bump types-netaddr from 0.9.0.1 to 0.10.0.20240106. ([\#16839](https://github.com/element-hq/synapse/issues/16839))
* Bump typing-extensions from 4.8.0 to 4.9.0. ([\#16815](https://github.com/element-hq/synapse/issues/16815))


# Synapse 1.99.0 (2024-01-16)

Synapse 1.99.0 is the first Synapse release under an AGPLv3.0 licence (with CLA to enable Element to sell AGPL
exceptions). You can read more about this here:

 - https://matrix.org/blog/2023/11/06/future-of-synapse-dendrite/
 - https://element.io/blog/element-to-adopt-agplv3/
 - https://element.io/blog/synapse-now-lives-at-github-com-element-hq-synapse/

No significant changes since 1.99.0rc1.


# Synapse 1.99.0rc1 (2024-01-09)

### Features

- Add [config options](https://element-hq.github.io/synapse/v1.99/usage/configuration/config_documentation.html#server_notices) to set the avatar and the topic of the server notices room, as well as the avatar of the server notices user. ([\#16679](https://github.com/matrix-org/synapse/issues/16679))
- Add config option [`email.notif_delay_before_mail`](https://element-hq.github.io/synapse/v1.99/usage/configuration/config_documentation.html#email) to tweak the delay before an email is sent following a notification. ([\#16696](https://github.com/matrix-org/synapse/issues/16696))
- Add new configuration option [`sentry.environment`](https://element-hq.github.io/synapse/v1.99/usage/configuration/config_documentation.html#sentry) for improved system monitoring. Contributed by @zeeshanrafiqrana. ([\#16738](https://github.com/matrix-org/synapse/issues/16738))
- Filter out rooms from the room directory being served to other homeservers when those rooms block that homeserver by their Access Control Lists. ([\#16759](https://github.com/element-hq/synapse/issues/16759))

### Bugfixes

- Fix a long-standing bug where the signing keys generated by Synapse were world-readable. Contributed by Fabian Klemp. ([\#16740](https://github.com/matrix-org/synapse/issues/16740))
- Fix email verification redirection. Contributed by Fadhlan Ridhwanallah. ([\#16761](https://github.com/element-hq/synapse/issues/16761))
- Fixed a bug that prevented users from being queried by display name if it contains non-ASCII characters. ([\#16767](https://github.com/element-hq/synapse/issues/16767))
- Allow reactivate user without password with Admin API in some edge cases. ([\#16770](https://github.com/element-hq/synapse/issues/16770))
- Adds the `recursion_depth` parameter to the response of the /relations endpoint if MSC3981 recursion is being performed. ([\#16775](https://github.com/element-hq/synapse/issues/16775))

### Improved Documentation

- Added version picker for Synapse documentation. Contributed by @Dmytro27Ind. ([\#16533](https://github.com/matrix-org/synapse/issues/16533))
- Clarify that `password_config.enabled: "only_for_reauth"` does not allow new logins to be created using password auth. ([\#16737](https://github.com/matrix-org/synapse/issues/16737))
- Remove value from header in configuration documentation for `refresh_token_lifetime`. ([\#16763](https://github.com/element-hq/synapse/issues/16763))
- Add another custom statistics collection server to the documentation. Contributed by @loelkes. ([\#16769](https://github.com/element-hq/synapse/issues/16769))

### Internal Changes

- Remove run-once workflow after adding the version picker to the documentation. ([\#9453](https://github.com/element-hq/synapse/issues/9453))
- Update the implementation of [MSC2965](https://github.com/matrix-org/matrix-spec-proposals/pull/2965) (OIDC Provider discovery). ([\#16726](https://github.com/matrix-org/synapse/issues/16726))
- Move the rust stubs inline for better IDE integration. ([\#16757](https://github.com/element-hq/synapse/issues/16757))
- Fix sample config doc CI. ([\#16758](https://github.com/element-hq/synapse/issues/16758))
- Simplify event internal metadata class. ([\#16762](https://github.com/element-hq/synapse/issues/16762), [\#16780](https://github.com/element-hq/synapse/issues/16780))
- Sign the published docker image using [cosign](https://docs.sigstore.dev/). ([\#16774](https://github.com/element-hq/synapse/issues/16774))
- Port `EventInternalMetadata` class to Rust. ([\#16782](https://github.com/element-hq/synapse/issues/16782))



### Updates to locked dependencies

* Bump actions/setup-go from 4 to 5. ([\#16749](https://github.com/matrix-org/synapse/issues/16749))
* Bump actions/setup-python from 4 to 5. ([\#16748](https://github.com/matrix-org/synapse/issues/16748))
* Bump immutabledict from 3.0.0 to 4.0.0. ([\#16743](https://github.com/matrix-org/synapse/issues/16743))
* Bump isort from 5.12.0 to 5.13.0. ([\#16745](https://github.com/matrix-org/synapse/issues/16745))
* Bump isort from 5.13.0 to 5.13.1. ([\#16752](https://github.com/matrix-org/synapse/issues/16752))
* Bump pydantic from 2.5.1 to 2.5.2. ([\#16747](https://github.com/matrix-org/synapse/issues/16747))
* Bump ruff from 0.1.6 to 0.1.7. ([\#16746](https://github.com/matrix-org/synapse/issues/16746))
* Bump types-setuptools from 68.2.0.2 to 69.0.0.0. ([\#16744](https://github.com/matrix-org/synapse/issues/16744))
