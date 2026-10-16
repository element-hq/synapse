#!/usr/bin/env bash
# Step 4: Alice joins the room again, from her new server, with the same key.

. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_state

say "Step 4: join the room again from Alice's second server"
explain "This is an ordinary /join. No migration code runs: the server looks for a"
explain "member key for this user in this room, finds the one that was just"
explain "imported, and uses it. Because that key is already a joined member of the"
explain "room, the join it sends is a membership event for an existing member —"
explain "signed by the same key as before, but now carrying content.domain of the"
explain "new server, co-signed by the new server's signing key."
explain ""
explain "Alice's old server is not asked, and could not refuse."

run "${CURL[@]}" -XPOST \
    "$HS3_CLIENT/_matrix/client/v3/join/$ROOM_ID?server_name=$HS2_NAME" \
    -H "Authorization: Bearer $ALICE3_TOKEN" -d '{}' -o /dev/null
explain "(joined via Bob's server, since Alice's first server is no longer the one"
explain "that has to be reachable for this to work.)"
sleep 3

say "The membership event the room now holds for that key"
sqlite3 -header -column "$HS2_DB" "
    SELECT json_extract(ej.json, '\$.content.localpart') AS localpart,
           json_extract(ej.json, '\$.content.domain')    AS domain,
           json_extract(ej.json, '\$.content.membership') AS membership
    FROM current_state_events c
    JOIN event_json ej ON ej.event_id = c.event_id
    WHERE c.room_id = '$ROOM_ID' AND c.type = 'm.room.member'
      AND c.state_key = '$ALICE_KEY';"
explain "Same member key, new domain. Bob's server verified that claim by fetching"
explain "the new server's current signing key and checking its signature on this"
explain "event, which is why the link below is verified rather than merely claimed."

say "Alice says something from her new server"
"${CURL[@]}" -XPUT "$HS3_CLIENT/_matrix/client/v3/rooms/$ROOM_ID/send/m.room.message/a2" \
    -H "Authorization: Bearer $ALICE3_TOKEN" \
    -d '{"msgtype":"m.text","body":"Same me, new server"}' -o /dev/null
sleep 2

say "What Bob received"
explain "If the link had not verified, this message would have been soft failed:"
explain "accepted into the room but not shown to clients, because a member whose"
explain "server has not vouched for them could be anybody."
"${CURL[@]}" "$HS2_CLIENT/_matrix/client/v3/rooms/$ROOM_ID/messages?dir=b&limit=6" \
    -H "Authorization: Bearer $BOB_TOKEN" \
  | "$PYTHON" -c '
import json, sys
for e in reversed(json.load(sys.stdin)["chunk"]):
    if e["type"] == "m.room.message":
        who = e.get("unsigned", {}).get("org.matrix.msc4428.stable_id", e["sender"])
        print("  %s: %s" % (who, e["content"]["body"]))
'

say "And Bob replies, which now has to reach a different server"
"${CURL[@]}" -XPUT "$HS2_CLIENT/_matrix/client/v3/rooms/$ROOM_ID/send/m.room.message/b2" \
    -H "Authorization: Bearer $BOB_TOKEN" \
    -d '{"msgtype":"m.text","body":"Welcome back"}' -o /dev/null
sleep 3
"${CURL[@]}" "$HS3_CLIENT/_matrix/client/v3/rooms/$ROOM_ID/messages?dir=b&limit=2" \
    -H "Authorization: Bearer $ALICE3_TOKEN" \
  | "$PYTHON" -c '
import json, sys
chunk = [e for e in json.load(sys.stdin)["chunk"] if e["type"] == "m.room.message"]
print("  Alice on her new server received:" if chunk else "  (nothing arrived)")
for e in chunk:
    print("    %s" % (e["content"]["body"],))
'
explain "Bob's server worked out where to send that from the domain Alice's key now"
explain "claims. Her first server is no longer in the room's routing at all."

say "Done. Run demo/msc4430/inspect.sh to compare with before."
