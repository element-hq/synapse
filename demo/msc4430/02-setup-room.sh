#!/usr/bin/env bash
# Step 2: build the room the demo moves an account out of.

. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

say "Step 2: three accounts, one member key room, two servers in it"
explain "Alice has accounts on two servers: the one she starts on (localhost:8480)"
explain "and the one she will move to (localhost:8482). Bob is on a third"
explain "(localhost:8481) and never moves: he is the bystander whose view of the"
explain "room is what the demo is really about, because he is the one who has to"
explain "work out, from the room's events alone, who Alice is."

say "Registering users"
ALICE1_TOKEN=$(register "$HS1_CLIENT" alice "$DEMO_PASSWORD")
note "@alice:$HS1_NAME registered on $HS1_CLIENT"
BOB_TOKEN=$(register "$HS2_CLIENT" bob "$DEMO_PASSWORD")
note "@bob:$HS2_NAME registered on $HS2_CLIENT"
ALICE3_TOKEN=$(register "$HS3_CLIENT" alice "$DEMO_PASSWORD")
note "@alice:$HS3_NAME registered on $HS3_CLIENT (the destination account)"

say "Creating a public member key room on Alice's first server"
explain "Room version $ROOM_VERSION is MSC4242's state DAG plus member keys. The"
explain "room is public and unencrypted on purpose: a public room needs no invite"
explain "(so the demo does not depend on federated invites), and encryption would"
explain "break across the move anyway, since cross-signing and one-time-key"
explain "signatures are made over a user ID that is about to change."
ROOM_ID=$("${CURL[@]}" -XPOST "$HS1_CLIENT/_matrix/client/v3/createRoom" \
    -H "Authorization: Bearer $ALICE1_TOKEN" \
    -d "{\"preset\":\"public_chat\",\"room_version\":\"$ROOM_VERSION\",\"name\":\"Portable accounts\"}" \
    | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["room_id"])')
note "room is $ROOM_ID"

say "What Alice looks like in her own room"
explain "Her events do not name her user ID anywhere. The create event's sender is"
explain "a member key: a per-user, per-room Ed25519 public key, which is also what"
explain "signs her events. Only her m.room.member event says who the key belongs"
explain "to, in content.localpart and content.domain, and only her server's"
explain "signature on that event makes the claim worth anything."
ALICE_KEY=$(sql "$HS1_DB" \
    "SELECT member_key FROM msc4430_member_keys WHERE room_id='$ROOM_ID' AND user_id='@alice:$HS1_NAME';")
note "Alice's member key in this room: $ALICE_KEY"

say "Bob joins over federation"
explain "His server has never seen this room, so it does not know the room version"
explain "until /make_join answers — and therefore cannot know whether to name Bob"
explain "by user ID or by member key. It sends both: the user ID in the path and a"
explain "member key in the org.matrix.msc4430.member_key query parameter, which a"
explain "server that has never heard of MSC4430 simply ignores."
run "${CURL[@]}" -XPOST \
    "$HS2_CLIENT/_matrix/client/v3/join/$ROOM_ID?server_name=$HS1_NAME" \
    -H "Authorization: Bearer $BOB_TOKEN" -d '{}' -o /dev/null
sleep 2

say "Saying something, both ways"
"${CURL[@]}" -XPUT "$HS1_CLIENT/_matrix/client/v3/rooms/$ROOM_ID/send/m.room.message/a1" \
    -H "Authorization: Bearer $ALICE1_TOKEN" \
    -d '{"msgtype":"m.text","body":"Hello from my first server"}' -o /dev/null
"${CURL[@]}" -XPUT "$HS2_CLIENT/_matrix/client/v3/rooms/$ROOM_ID/send/m.room.message/b1" \
    -H "Authorization: Bearer $BOB_TOKEN" \
    -d '{"msgtype":"m.text","body":"Hi Alice"}' -o /dev/null
sleep 2

cat > "$STATE_FILE" <<STATE
ROOM_ID='$ROOM_ID'
ALICE_KEY='$ALICE_KEY'
ALICE1_TOKEN='$ALICE1_TOKEN'
ALICE3_TOKEN='$ALICE3_TOKEN'
BOB_TOKEN='$BOB_TOKEN'
STATE
note "wrote $STATE_FILE"

print_logins "$ROOM_ID"

say "Done. Run demo/msc4430/inspect.sh to see where things stand, then 03-move-account.sh"
