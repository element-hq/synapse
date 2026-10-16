#!/usr/bin/env bash
# Show who each server thinks the room's members are. Run it before and after
# the move: the difference is the demo.

. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_state

say "What Bob's server says the room's members are"
explain "MSC4428's member_info map is how a client learns which user a stable ID"
explain "stands for. In a member key room the stable ID is the member key, and the"
explain "entry is what Bob's server worked out from the room's events: the user a"
explain "verified link names, or null when nothing it can check vouches for the key."
"${CURL[@]}" "$HS2_CLIENT/_matrix/client/v3/rooms/$ROOM_ID/members" \
    -H "Authorization: Bearer $BOB_TOKEN" \
  | "$PYTHON" -c '
import json, sys
body = json.load(sys.stdin)
info = body.get("org.matrix.msc4428.member_info", {})
for chunk in body.get("chunk", []):
    key = chunk["state_key"]
    entry = info.get(key, {})
    user = entry.get("user_id", "(no entry: not a member key)")
    print("  %s  ->  %s   [%s]" % (key, user, chunk["content"].get("membership")))
'

say "The links Bob's server has checked"
explain "One row per member key that is not its own, holding the domain claimed,"
explain "the localpart claimed, and whether that domain's *current* signing key was"
explain "seen to vouch for it. The domain is used for routing whether verified or"
explain "not; only a verified link decides who the key is and what that server is"
explain "allowed to be told about the room."
sqlite3 -header -column "$HS2_DB" \
    "SELECT member_key, domain, localpart, verified FROM msc4430_member_links;" || true

say "Where Alice's key is held"
explain "A member key's private half lives in msc4430_member_keys on whichever"
explain "server holds the account. That table is the entire portable part of an"
explain "account: everything else about the room is re-derived from its events."
for pair in "$HS1_NAME|$HS1_DB" "$HS2_NAME|$HS2_DB" "$HS3_NAME|$HS3_DB"; do
    name="${pair%%|*}"; db="${pair##*|}"
    held=$(sql "$db" "SELECT user_id FROM msc4430_member_keys WHERE member_key='$ALICE_KEY';" || true)
    if [ -n "$held" ]; then
        note "$name holds it, as $held"
    else
        note "$name does not hold it"
    fi
done

say "Which servers the room routes to, from Bob's server"
explain "Member keys name no server, so hosts-in-room is the set of domains the"
explain "joined members' links claim. This is what decides where Bob's next"
explain "message is sent."
sqlite3 "$HS2_DB" "
    SELECT DISTINCT l.domain
    FROM current_state_events c
    JOIN msc4430_member_links l
      ON l.room_id = c.room_id AND l.member_key = c.state_key
    WHERE c.room_id = '$ROOM_ID' AND c.type = 'm.room.member' AND c.membership = 'join'
    UNION
    SELECT '$HS2_NAME' WHERE EXISTS (
        SELECT 1 FROM msc4430_member_keys k
        JOIN current_state_events c2 ON c2.state_key = k.member_key
        WHERE k.room_id = '$ROOM_ID' AND c2.room_id = '$ROOM_ID'
          AND c2.type = 'm.room.member' AND c2.membership = 'join');" \
  | sed 's/^/  /'
