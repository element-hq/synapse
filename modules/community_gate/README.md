# Community Gate Module for Matrix Synapse

Isolates **external communities** from the company's internal rooms on the
same homeserver. Staff keep full access; members of an external community
(e.g. a creator's audience, invited with registration tokens) can only reach
that community's space and the rooms inside it.

The isolation is policy, not structure: it is enforced through Synapse's
spam-checker callbacks, which run for every join, invite and room creation —
including joins made through the admin API (`/_synapse/admin/v1/join`), which
run as the target user. That is what closes the `room_service` shortcuts
(`invite`, `invite_space`) as well.

## Who is internal

A user is **internal** if they are a server admin, or if they have an
external ID from `internal_auth_provider` (default `oidc-google`, i.e. they
logged in through the company's Google Workspace SSO). Everyone else is
**community**.

This only holds while the OIDC client stays restricted to the company's
domain (Google "Internal" client). Opening that client to any Google account
would make every Google user internal.

## Rules

- **Join** — internal: anywhere. Community: a space listed in
  `community_spaces`, any room reachable from it through `m.space.child`
  (nested subspaces included, up to 5 levels; children with empty content
  are treated as removed). A child only counts if it also has an
  `m.space.parent` event pointing back at that space: the community owner
  has power in the space and could publish `m.space.child` for any room,
  including internal ones, but `m.space.parent` lives in the child room
  itself, so only someone with power there can confirm the link. Rooms
  created outside Element's "create room in space" flow (e.g. via
  `room_service`) must get their `m.space.parent` set by an admin. Also
  allowed: a room whose active members (`join`/`invite`)
  are all community — that is what lets community members DM each other.
  An empty room never counts as community-only.
- **Invite** — free on the same side of the boundary (internal → internal,
  community → community). Crossing the boundary, in either direction, only
  into a community room. This also prevents dangling invites that would show
  an internal room's name to a community member.
- **Create room** — internal: anything. Community: direct messages only
  (`is_direct: true`, not a space). Users listed in `community_owners`
  (e.g. the creator who runs the community) may also create rooms and
  subspaces; for everything else they are still community. Rooms they create
  through Element's "add room to space" flow get both `m.space.child` and
  `m.space.parent`, so they count as community rooms.
- **Jitsi widget** — refused (`check_event_for_spam`) in community rooms and
  in community-only DMs, whoever sends it. The company's Jitsi transcribes
  every conference and the transcript ends up in the internal knowledge base,
  so community rooms get no Jitsi calls at all; without the widget there is
  no call, whatever the client. Other widgets and widget removals pass. This
  callback runs for every event, so anything that isn't a Jitsi widget
  returns before any lookup.

Denials return `M_FORBIDDEN` and are logged at `INFO`.

## Internal space auto-join (optional)

When `internal_space` is set, staff join it automatically on login
(`on_user_login` account-validity callback): if the login came from
`internal_auth_provider` and the user isn't a member yet, `admin_user_id`
invites them (unless an invite is already pending) and they join. Internal
rooms are `restricted` to members of that space, so this is what lets new
hires see them. The SSO handler records the external ID before this callback
runs, so first logins count as internal. Failures are logged and never block
the login.

## Cross-module use

On load the module stores its rule at `api._hs.community_gate`.
`room_service` reads it at call time (load order doesn't matter):

- `discover` requires authentication and hides non-community rooms from
  community members;
- `invite` (keyword) and `invite_space` answer `403` before attempting the
  admin join.

`bundle_service.invite_bundle` skips rooms the gate denies, for the same
reason: the admin join invites before the join is refused, which would leave
a dangling invite carrying an internal room's name.

Without this module loaded, `room_service` behaves as before.

## Configuration

```yaml
modules:
  - module: modules.community_gate.module.CommunityGateModule
    config:
      internal_auth_provider: oidc-google   # optional, default oidc-google
      community_spaces:
        - "!communitySpace:matrix.example.com"
      # optional — staff auto-join on login
      internal_space: "!companySpace:matrix.example.com"
      admin_user_id: "@admin:matrix.example.com"   # who sends the invite
      # optional — community owners who may create rooms and subspaces
      community_owners:
        - "@creator:matrix.example.com"
```

Recommended alongside it in `homeserver.yaml`:

```yaml
limit_profile_requests_to_users_who_share_rooms: true
require_auth_for_profile_requests: true
```

## Tests

The rule (`gate.py`) has no Synapse dependency and is tested against an
in-memory backend. The wiring (`module.py`) needs an installed `synapse`
package, so run the tests with a `PYTHONPATH` that exposes `modules/` but not
the repository's own `synapse/` source tree:

```sh
mkdir -p /tmp/pp && ln -sfn "$PWD/modules" /tmp/pp/modules
cd /tmp/pp && PYTHONPATH=/tmp/pp pytest -vv modules/community_gate/tests/
```
