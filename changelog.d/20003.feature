Add optional support for [MSC4262: Profile Updates for Sliding Sync](https://github.com/matrix-org/matrix-spec-proposals/pull/4262).
Currently defaults to not enabled, and is limited to local users only for the sync results.

Additionally, optional support for [MSC4429: Profile Updates for Legacy Sync](https://github.com/matrix-org/matrix-spec-proposals/pull/4429)
now includes removed fields in the `removed_profile_fields` response key, in addition to setting the field to a `null`
value. The latter behaviour will be removed in a future Synapse release.