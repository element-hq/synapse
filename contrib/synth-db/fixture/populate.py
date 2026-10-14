#!/usr/bin/env python3
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
"""Populate a throwaway Synapse with a small, varied population so that
`synth-db profile` has something to read and the leak-check test has
something to catch.

Point it at a homeserver configured with:

    server_name: <anything>
    enable_registration: true
    enable_registration_without_verification: true
    app_service_config_files: [<a registration file with as_token
                                "as_token_fixture" and users regex "@as_.*">]
    rc_message / rc_registration / rc_login / rc_joins / rc_invites:
        generous limits, or expect to wait on the 429 retries

Usage: populate.py [base_url]

The appservice is used to backdate messages (the `ts` parameter) so that
event rates spread over the last 20 days instead of landing in one minute.
The script is not idempotent; start from an empty database.
"""

import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8008"
AS_TOKEN = "as_token_fixture"
random.seed(42)
NOW = int(time.time() * 1000)
DAY = 86_400_000


def req(
    method: str,
    path: str,
    body: Any = None,
    token: str | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    for _ in range(20):
        try:
            with urllib.request.urlopen(r) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            text = e.read().decode()
            if e.code == 429:
                time.sleep(json.loads(text).get("retry_after_ms", 500) / 1000 + 0.05)
                continue
            raise RuntimeError(f"{method} {path}: {e.code} {text}") from None
    raise RuntimeError(f"{method} {path}: still rate limited")


def register(name: str) -> Any:
    return req(
        "POST",
        "/_matrix/client/v3/register",
        {"username": name, "password": "pw", "auth": {"type": "m.login.dummy"}},
    )


def login(name: str) -> Any:
    return req(
        "POST",
        "/_matrix/client/v3/login",
        {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": name},
            "password": "pw",
        },
    )


# Users: some with several devices, some with keys, account data, filters,
# push rules and ignored users.
users: dict[str, str] = {}
for i in range(40):
    r = register(f"user{i}")
    users[r["user_id"]] = r["access_token"]
    for _ in range(random.choice([0, 0, 1, 2, 4])):
        login(f"user{i}")
uids = list(users)

as_users: list[str] = []
for i in range(10):
    r = req(
        "POST",
        "/_matrix/client/v3/register",
        {"type": "m.login.application_service", "username": f"as_{i}"},
        token=AS_TOKEN,
    )
    as_users.append(r["user_id"])

for i, (uid, tok) in enumerate(users.items()):
    if i % 3 != 0:
        req(
            "POST",
            "/_matrix/client/v3/keys/upload",
            {
                "one_time_keys": {
                    f"signed_curve25519:k{j}": {"key": "x" * 43, "signatures": {}}
                    for j in range(random.randint(1, 50))
                },
                "fallback_keys": {
                    "signed_curve25519:fb": {
                        "key": "y" * 43,
                        "fallback": True,
                        "signatures": {},
                    }
                },
            },
            token=tok,
        )
    req("PUT", f"/_matrix/client/v3/user/{uid}/account_data/m.direct", {}, token=tok)
    req(
        "PUT",
        f"/_matrix/client/v3/user/{uid}/account_data/im.vector.setting.breadcrumbs",
        {"recent_rooms": []},
        token=tok,
    )
    if i % 4 == 0:
        req(
            "PUT",
            f"/_matrix/client/v3/user/{uid}/account_data/org.example.custom",
            {"a": "b" * 500},
            token=tok,
        )
    if i % 5 == 0:
        req(
            "POST",
            f"/_matrix/client/v3/user/{uid}/filter",
            {"room": {"timeline": {"limit": 20}}},
            token=tok,
        )
        req(
            "PUT",
            f"/_matrix/client/v3/pushrules/global/override/custom.{i}",
            {
                "conditions": [
                    {"kind": "event_match", "key": "content.body", "pattern": "x"}
                ],
                "actions": ["notify"],
            },
            token=tok,
        )
    if i % 7 == 0:
        req(
            "PUT",
            f"/_matrix/client/v3/user/{uid}/account_data/m.ignored_user_list",
            {"ignored_users": {uids[(i + 1) % len(uids)]: {}}},
            token=tok,
        )

# Rooms: a space, public rooms of varied size, private rooms (half
# encrypted), DMs, and some pending invites.
rooms: list[tuple[str, list[str], str]] = []


def create_room(creator: str, kind: str, **extra: Any) -> str:
    body = {"visibility": "public" if kind == "public" else "private"}
    body.update(extra)
    return req("POST", "/_matrix/client/v3/createRoom", body, token=users[creator])[
        "room_id"
    ]


def join(room: str, uid: str) -> None:
    req("POST", f"/_matrix/client/v3/join/{room}", {}, token=users[uid])


def invite(room: str, by: str, uid: str) -> None:
    req(
        "POST",
        f"/_matrix/client/v3/rooms/{room}/invite",
        {"user_id": uid},
        token=users[by],
    )


space = create_room(
    uids[0], "space", creation_content={"type": "m.space"}, name="Space"
)
for u in uids[1:6]:
    invite(space, uids[0], u)
    join(space, u)
rooms.append((space, uids[0:6], "space"))

for n, size in enumerate([35, 25, 15, 10, 8, 6, 5, 4]):
    rid = create_room(
        uids[n], "public", name=f"Public {n}", topic="t" * 50, room_alias_name=f"pub{n}"
    )
    members = [uids[n]]
    for u in random.sample([x for x in uids if x != uids[n]], size - 1):
        join(rid, u)
        members.append(u)
    for a in as_users[: n + 1]:
        req(
            "POST",
            f"/_matrix/client/v3/join/{rid}",
            {},
            token=AS_TOKEN,
            params={"user_id": a},
        )
    if n < 3:
        req(
            "PUT",
            f"/_matrix/client/v3/rooms/{space}/state/m.space.child/{rid}",
            {"via": [uids[0].split(":", 1)[1]]},
            token=users[uids[0]],
        )
    rooms.append((rid, members, "public"))

for n in range(12):
    creator = random.choice(uids)
    enc = n % 2 == 0
    extra = (
        {
            "initial_state": [
                {
                    "type": "m.room.encryption",
                    "state_key": "",
                    "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                }
            ]
        }
        if enc
        else {}
    )
    rid = create_room(creator, "private", name=f"Private {n}", **extra)
    members = [creator]
    for u in random.sample([x for x in uids if x != creator], random.randint(2, 8)):
        invite(rid, creator, u)
        join(rid, u)
        members.append(u)
    rooms.append((rid, members, "encrypted" if enc else "private"))

for _ in range(15):
    a, b = random.sample(uids, 2)
    rid = create_room(
        a, "dm", is_direct=True, invite=[b], preset="trusted_private_chat"
    )
    join(rid, b)
    rooms.append((rid, [a, b], "dm"))
    for u in (a, b):
        req(
            "PUT",
            f"/_matrix/client/v3/user/{u}/account_data/m.direct",
            {u if u != a else b: [rid]},
            token=users[u],
        )

for _i in range(6):
    rid, members, _kind = random.choice(rooms[1:9])
    cand = [u for u in uids if u not in members]
    if cand:
        invite(rid, members[0], random.choice(cand))


# Messages spread over the last 20 days. Only appservices may backdate, so
# public rooms (which have appservice members) get backdated traffic and
# the rest is sent "now" by ordinary members.
def send(room: str, sender: str, ts: int, msgtype: str, content: dict[str, Any]) -> str:
    txn = f"{ts}-{random.random()}"
    return req(
        "PUT",
        f"/_matrix/client/v3/rooms/{room}/send/{msgtype}/{txn}",
        content,
        token=AS_TOKEN,
        params={"user_id": sender, "ts": ts},
    )["event_id"]


last_event: dict[str, str] = {}
for i, (rid, members, kind) in enumerate(rooms):
    rate = {"space": 0, "public": 60, "private": 25, "encrypted": 25, "dm": 12}[kind]
    n_msgs = random.randint(rate // 2, rate) if rate else 0
    for _ in range(n_msgs):
        if kind == "public":
            sender = random.choice(as_users[:i]) if i > 0 else as_users[0]
            ts = NOW - random.randint(0, 20 * DAY)
            ev = send(
                rid,
                sender,
                ts,
                "m.room.message",
                {"msgtype": "m.text", "body": "m" * random.randint(5, 800)},
            )
        else:
            sender = random.choice(members)
            txn = f"{random.random()}"
            if kind == "encrypted":
                content = {
                    "algorithm": "m.megolm.v1.aes-sha2",
                    "ciphertext": "c" * random.randint(100, 4000),
                    "sender_key": "k",
                    "session_id": "s",
                }
                ev = req(
                    "PUT",
                    f"/_matrix/client/v3/rooms/{rid}/send/m.room.encrypted/{txn}",
                    content,
                    token=users[sender],
                )["event_id"]
            else:
                ev = req(
                    "PUT",
                    f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/{txn}",
                    {"msgtype": "m.text", "body": "m" * random.randint(5, 800)},
                    token=users[sender],
                )["event_id"]
        last_event[rid] = ev
    if kind in ("public", "private") and n_msgs and random.random() < 0.5:
        root = last_event[rid]
        sender = random.choice(members)
        for j in range(3):
            req(
                "PUT",
                f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/thr{j}-{random.random()}",
                {
                    "msgtype": "m.text",
                    "body": "reply",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": root},
                },
                token=users[sender],
            )

# Receipts, tags, read markers, a few leaves, a ban, some presence.
for rid, members, kind in rooms:
    if rid not in last_event:
        continue
    for u in members:
        r = random.random()
        if r < 0.5:
            req(
                "POST",
                f"/_matrix/client/v3/rooms/{rid}/receipt/m.read/{last_event[rid]}",
                {},
                token=users[u],
            )
        elif r < 0.7:
            req(
                "POST",
                f"/_matrix/client/v3/rooms/{rid}/receipt/m.read.private/{last_event[rid]}",
                {},
                token=users[u],
            )
        if random.random() < 0.2:
            req(
                "PUT",
                f"/_matrix/client/v3/user/{u}/rooms/{rid}/tags/m.favourite",
                {"order": 0.5},
                token=users[u],
            )
        if random.random() < 0.1:
            req(
                "PUT",
                f"/_matrix/client/v3/user/{u}/rooms/{rid}/tags/m.lowpriority",
                {},
                token=users[u],
            )
        if random.random() < 0.3:
            req(
                "POST",
                f"/_matrix/client/v3/rooms/{rid}/read_markers",
                {"m.fully_read": last_event[rid]},
                token=users[u],
            )
        if kind == "public" and random.random() < 0.15 and u != members[0]:
            req("POST", f"/_matrix/client/v3/rooms/{rid}/leave", {}, token=users[u])

big = rooms[1]
req(
    "POST",
    f"/_matrix/client/v3/rooms/{big[0]}/ban",
    {"user_id": big[1][-1]},
    token=users[big[1][0]],
)
for u in uids[:5]:
    req(
        "PUT",
        f"/_matrix/client/v3/presence/{u}/status",
        {"presence": "online"},
        token=users[u],
    )

print("rooms:", len(rooms), "users:", len(users) + len(as_users))
