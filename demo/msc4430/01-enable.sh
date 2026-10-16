#!/usr/bin/env bash
# Step 1: turn MSC4430 on in the three demo homeservers.

. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_synapse_python

say "Step 1: enable MSC4430 on all three servers"
explain "demo/start.sh writes a config per server but knows nothing about member"
explain "keys. Member key rooms are gated twice over: the experimental flag has to"
explain "be on for the room version to be registered at all, and the room version"
explain "then gates every code path that treats members as keys. So all three"
explain "servers need the flag, or the two that have it will be talking about a"
explain "room version the third has never heard of."

for port in 8080 8081 8082; do
    config="$DEMO_DIR/$port/$port.config"
    if [ ! -f "$config" ]; then
        warn "$config does not exist — run demo/start.sh once first."
        exit 1
    fi
    if grep -q "msc4430_enabled" "$config"; then
        note "$port already has the flag, leaving it alone"
        continue
    fi
    note "adding experimental_features.msc4430_enabled to $config"
    {
        printf '\n# Added by demo/msc4430/01-enable.sh\n'
        printf 'experimental_features:\n'
        printf '  msc4430_enabled: true\n'
    } >> "$config"
done

say "Restarting the servers so they pick it up"
explain "The room version is registered at config load, so a running server will"
explain "not notice the new flag. stop.sh kills every demo server it finds a pid"
explain "file for; start.sh will not regenerate the configs it has already"
explain "customised, so restarting is safe and keeps the databases."
run "$DEMO_DIR/stop.sh" || true
sleep 2
run "$DEMO_DIR/start.sh" "${1:---no-rate-limit}"
sleep 5

say "Checking all three are up and advertising MSC4430"
for client in "$HS1_CLIENT" "$HS2_CLIENT" "$HS3_CLIENT"; do
    versions=$("${CURL[@]}" "$client/_matrix/client/versions" || true)
    if printf '%s' "$versions" | grep -q "org.matrix.msc4430"; then
        note "$client advertises org.matrix.msc4430"
    else
        warn "$client does NOT advertise org.matrix.msc4430"
        warn "response was: $versions"
        exit 1
    fi
done

say "Done. Next: demo/msc4430/02-setup-room.sh"
