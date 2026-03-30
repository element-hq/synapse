# Implementing experimental features in Synapse

It can be desirable to implement "experimental" features which are disabled by
default and must be explicitly enabled via the Synapse configuration. This is
applicable for features which:

* Are unstable in the Matrix spec (e.g. those defined by an MSC that has not yet been merged).
* Developers are not confident in their use by general Synapse administrators/users
  (e.g. a feature is incomplete, buggy, performs poorly, or needs further testing).

Note that this only really applies to features which are expected to be desirable
to a broad audience. The [module infrastructure](../modules/index.md) should
instead be investigated for non-standard features.

Guarding experimental features behind configuration flags should help with some
of the following scenarios:

* Ensure that clients do not assume that unstable features exist (failing
  gracefully if they do not).
* Unstable features do not become de-facto standards and can be removed
  aggressively (since only those who have opted-in will be affected).
* Ease finding the implementation of unstable features in Synapse (for future
  removal or stabilization).
* Ease testing a feature (or removal of feature) due to enabling/disabling without
  code changes. It also becomes possible to ask for wider testing, if desired.

Experimental configuration flags should be disabled by default (requiring Synapse
administrators to explicitly opt-in), although there are situations where it makes
sense (from a product point-of-view) to enable features by default. This is
expected and not an issue.

It is not a requirement for experimental features to be behind a configuration flag,
but one should be used if unsure.

New experimental configuration flags should be added under the `experimental_features`
configuration key (see the `synapse.config.experimental` file) and either explain
(briefly) what is being enabled, or include the MSC number.
The configuration flag should link to the tracking issue for the experimental feature (see below).


## Tracking issues for experimental features

In the interest of having some documentation around experimental features, without
polluting the stable documentation, all new experimental features should have a tracking issue with
[the `T-ExperimentalFeature` label](https://github.com/element-hq/synapse/issues?q=sort%3Aupdated-desc+state%3Aopen+label%3A%22T-ExperimentalFeature%22),
kept open as long as the experimental feature is present in Synapse.

The configuration option for the feature should have a comment linking to the tracking issue,
for ease of discoverability.

As a guideline, the issue should contain:

- Context for why this experimental feature is in Synapse
  - This could well be a link to somewhere else, where this context is already available.
- If applicable, why the feature is enabled by default. (Why do we need to enable it by default and why is it safe?)
- If applicable, setup instructions for any non-standard components or configuration needed by the feature.
  (Ideally this will be moved to the configuration manual after stabilisation.)
- Design decisions behind the Synapse implementation.
  (Ideally this will be moved to the developers' documentation after stabilisation.)
- Any caveats around the current implementation of the feature, such as:
  - missing aspects
  - breakage or incompatibility that is expected if/when the feature is stabilised,
    or when the feature is turned on/off
- Criteria for how we know whether we can remove the feature in the future.
