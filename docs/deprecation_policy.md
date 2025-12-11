# Deprecation Policy

Synapse has a number of **platform dependencies** (Python, Rust, PostgreSQL, and SQLite)
and **application dependencies** (Python and Rust packages). This document outlines the
policy towards which versions we support, and when we drop support for versions in the
future.

## Platform Dependencies

Synapse follows the upstream support life cycles for Python and PostgreSQL,
i.e. when a version reaches End of Life Synapse will withdraw support for that
version in future releases.

Details on the upstream support life cycles for Python and PostgreSQL are
documented at [https://endoflife.date/python](https://endoflife.date/python) and
[https://endoflife.date/postgresql](https://endoflife.date/postgresql).

A Rust compiler is required to build Synapse from source. For any given release
the minimum required version may be bumped up to a recent Rust version, and so
people building from source should ensure they can fetch recent versions of Rust
(e.g. by using [rustup](https://rustup.rs/)).

The oldest supported version of SQLite is the version
[provided](https://packages.debian.org/oldstable/libsqlite3-0) by
[Debian oldstable](https://wiki.debian.org/DebianOldStable).


### Context

It is important for system admins to have a clear understanding of the platform
requirements of Synapse and its deprecation policies so that they can
effectively plan upgrading their infrastructure ahead of time. This is
especially important in contexts where upgrading the infrastructure requires
auditing and approval from a security team, or where otherwise upgrading is a
long process.

By following the upstream support life cycles Synapse can ensure that its
dependencies continue to get security patches, while not requiring system admins
to constantly update their platform dependencies to the latest versions.

For Rust, the situation is a bit different given that a) the Rust foundation
does not generally support older Rust versions, and b) the library ecosystem
generally bump their minimum support Rust versions frequently. In general, the
Synapse team will try to avoid updating the dependency on Rust to the absolute
latest version, but introducing a formal policy is hard given the constraints of
the ecosystem.

On a similar note, SQLite does not generally have a concept of "supported 
release"; bugfixes are published for the latest minor release only. We chose to
track Debian's oldstable as this is relatively conservative, predictably updated
and is consistent with the `.deb` packages released by Matrix.org.


## Application dependencies

For application-level Python dependencies, we often specify loose version constraints
(ex. `>=X.Y.Z`) to be forwards compatible with any new versions. Upper bounds (`<A.B.C`)
are only added when necessary to prevent known incompatibilities.

When selecting a minimum version, while we are mindful of the impact on downstream
package maintainers, our primary focus is on the maintainability and progress of Synapse
itself.

For developers, a Python dependency version can be considered a "no-brainer" upgrade once it is
available in both the latest [Debian Stable](https://packages.debian.org/stable/) and
[Ubuntu LTS](https://launchpad.net/ubuntu) repositories. No need to burden yourself with
extra scrutiny or consideration at this point.

We aggressively update Rust dependencies. Since these are statically linked and managed
entirely by `cargo` during build, they *can* pose no ongoing maintenance burden on others.
This allows us to freely upgrade to leverage the latest ecosystem advancements assuming
they don't have their own system-level dependencies.


### Context

Because Python dependencies can easily be managed in a virtual environment, we are less
concerned about the criteria for selecting minimum versions. The only thing of concern
is making sure we're not making it unnecessarily difficult for downstream package
maintainers. Generally, this just means avoiding the bleeding edge for a few months.

The situation for Rust dependencies is fundamentally different. For packagers, the
concerns around Python dependency versions do not apply. The `cargo` tool handles
downloading and building all libraries to satisfy dependencies, and these libraries are
statically linked into the final binary. This means that from a packager's perspective,
the Rust dependency versions are an internal build detail, not a runtime dependency to
be managed on the target system. Consequently, we have even greater flexibility to
upgrade Rust dependencies as needed for the project. Some distros (e.g. Fedora) do
package Rust libraries, but this appears to be the outlier rather than the norm.
