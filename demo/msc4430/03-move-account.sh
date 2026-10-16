#!/usr/bin/env bash
# Step 3: move Alice's member key from her first server to her second.
#
# This is the whole of "poor man's account portability": there is no migration
# protocol, no handshake and no consent. A member key is a private key in a
# table; whoever holds it is that member. Moving the row moves the member.

. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_synapse_python
require_state

[ "${1:-}" = "--yes" ] && ASSUME_YES=1

say "Step 3: hand Alice's member key to her second server"
explain "What is about to be copied is one row of msc4430_member_keys:"
explain ""
explain "    room_id      the room the key is for (keys are per user, per room)"
explain "    user_id      which local account holds it — remapped on the way across"
explain "    member_key   the public half: what the room sees as sender and state_key"
explain "    signing_key  the PRIVATE half, which signs this member's events"
explain ""
explain "Nothing else moves. The room's state, its history and what Alice's server"
explain "knows about everybody else are all re-derived from the room's events when"
explain "the destination joins, so they are not worth carrying."

say "The row, as it stands on Alice's first server"
sqlite3 -header -column "$HS1_DB" "
    SELECT room_id, user_id, member_key, substr(signing_key, 1, 6) || '...' AS signing_key_prefix
    FROM msc4430_member_keys
    WHERE room_id = '$ROOM_ID' AND user_id = '@alice:$HS1_NAME';"
explain "(only the first characters of the private key are shown: it is a secret,"
explain "and unlike an access token MSC4430 gives no way to revoke one.)"

SOURCE_ROW=$(sql "$HS1_DB" "
    SELECT signing_key || '|' || created_ts FROM msc4430_member_keys
    WHERE room_id = '$ROOM_ID' AND user_id = '@alice:$HS1_NAME';")
if [ -z "$SOURCE_ROW" ]; then
    warn "No key for @alice:$HS1_NAME in $ROOM_ID — has the move already been done?"
    exit 1
fi
SIGNING_KEY="${SOURCE_ROW%%|*}"
CREATED_TS="${SOURCE_ROW##*|}"

EXISTING=$(sql "$HS3_DB" "
    SELECT member_key FROM msc4430_member_keys
    WHERE room_id = '$ROOM_ID' AND user_id = '@alice:$HS3_NAME';")
if [ -n "$EXISTING" ]; then
    warn "The destination already holds a key for this room ($EXISTING)."
    warn "It must be imported BEFORE that server ever tries to join, or it will"
    warn "have minted one of its own and you would be moving a different member."
    exit 1
fi

say "Why the servers have to be stopped first"
explain "Both of them cache what they know about their own member keys, so editing"
explain "the database underneath a running server would leave it answering from a"
explain "cache that no longer matches. stop.sh stops all three; start.sh brings"
explain "them back with their databases intact."
confirm "Stop all three demo servers, move the key, and start them again?"

run "$DEMO_DIR/stop.sh" || true
sleep 2

say "Writing the key into Alice's second server"
explain "The user_id changes and nothing else does: the key is the same key, so to"
explain "the room this is the same member. That is the entire trick — the room"
explain "identifies members by key, and the key did not change."
sqlite3 "$HS3_DB" "
    INSERT INTO msc4430_member_keys (room_id, user_id, member_key, signing_key, created_ts)
    VALUES ('$ROOM_ID', '@alice:$HS3_NAME', '$ALICE_KEY', '$SIGNING_KEY', $CREATED_TS);"
note "@alice:$HS3_NAME now holds $ALICE_KEY"

say "Deleting it from Alice's first server"
explain "A move rather than a copy, because the honest version of this demo should"
explain "show what the MSC itself says: nothing stops the old server from keeping"
explain "the key and carrying on as that member. Deleting it is a courtesy the old"
explain "server extends, not a rule anyone enforces. Note also what we do NOT do —"
explain "deactivating Alice there would send a leave event signed with her key,"
explain "which would forget the link and undo the move."
sqlite3 "$HS1_DB" "
    DELETE FROM msc4430_member_keys
    WHERE room_id = '$ROOM_ID' AND user_id = '@alice:$HS1_NAME';"
note "removed from @alice:$HS1_NAME"

run "$DEMO_DIR/start.sh" --no-rate-limit
sleep 5

say "Nothing has changed in the room yet"
explain "No event has been sent, so Bob's server still believes Alice is at"
explain "$HS1_NAME and still routes the room's traffic there. The key has changed"
explain "hands; the room has not been told. Run inspect.sh to see that, then"
explain "04-rejoin.sh to tell it."
