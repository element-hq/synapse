# MSC4430 account portability demo

What it shows: a room member moving from one homeserver to another, taking their
identity in the room with them, while a third server that is in the room watches
it happen and keeps working.

It is the *insecure* form of account portability — the one MSC4430 introduces by
accident, and says so in its Potential Issues section. A member key is a private
key in a table; whoever holds it is that member. There is no migration protocol,
no handshake, and nothing stops the old server from keeping a copy. That is the
point of the demo: the mechanism is real and already works, and the reasons it is
not yet safe are structural rather than missing code.

## Running it

These scripts sit on top of `demo/start.sh`, which already makes three
homeservers that federate over localhost with self-signed certificates.

```sh
demo/start.sh --no-rate-limit     # once, to generate the configs
demo/msc4430/01-enable.sh         # add msc4430_enabled, restart
demo/msc4430/02-setup-room.sh     # three accounts, a member key room, two servers in it
demo/msc4430/inspect.sh           # the "before" picture
demo/msc4430/03-move-account.sh   # move Alice's member key to her second server
demo/msc4430/04-rejoin.sh         # join again from there, with the same key
demo/msc4430/inspect.sh           # the "after" picture
```

Pass `--yes` to `03-move-account.sh` to skip its confirmation prompt.

Each script explains what it is about to do before doing it, and `inspect.sh`
shows the three things that change. Run it before and after.

## The cast

| Role | Client API | Server name | Database |
|---|---|---|---|
| Alice's first server | `http://localhost:8080` | `localhost:8480` | `demo/8080/homeserver.db` |
| Bob's server (the bystander) | `http://localhost:8081` | `localhost:8481` | `demo/8081/homeserver.db` |
| Alice's second server | `http://localhost:8082` | `localhost:8482` | `demo/8082/homeserver.db` |

Bob is the interesting one. He never moves, and he is the one who has to work out
from the room's events alone who Alice is — which is what the whole of MSC4430 is
about.

Each server has two numbers and it is easy to mix them up: the client API is the
plain HTTP port, while the *server name* is the HTTPS one, because a server name
has to match the port its federation listener is on. So Alice is
`@alice:localhost:8480` but you log in to her at `http://localhost:8080`. Only
federation uses TLS; nothing in the client half of this demo needs a certificate.

`02-setup-room.sh` prints the homeserver URL, username and password for all three
accounts when it finishes, so you can take over from a browser.

## What to watch

Three things change when the move completes, and nothing else does:

1. **Identity.** `member_info` on Bob's server flips from `@alice:localhost:8480`
   to `@alice:localhost:8482` for the same member key.
2. **Routing.** Bob's server stops sending the room's traffic to Alice's first
   server and starts sending it to her second.
3. **Nothing else.** Her old messages are still hers — the sender on every one of
   them is the same key. No state resolution, no auth rule and no event ID
   changed, because the room never identified her by server in the first place.

## Honest caveats, worth saying out loud while demoing

* **The old server could just keep the key.** `03-move-account.sh` deletes it as a
  courtesy; nothing enforces that, and the old server could carry on sending
  events as Alice forever. Fixing that needs key revocation, which this MSC does
  not have (see its Account Portability appendix).
* **Encryption breaks.** Cross-signing and one-time-key signatures are made over
  a user ID, which is exactly what just changed. The demo room is unencrypted on
  purpose.
* **Do not deactivate the old account** to "finish" the move. Deactivation sends a
  leave event signed with Alice's member key, which forgets the link and undoes
  everything.
* **Import before the destination ever joins.** If that server has already minted
  a member key for the room, you are moving a different member, and the script
  refuses rather than making a mess.

## If you run it in more than one place

The configs under `demo/808x/` hold absolute paths, so a `demo/` directory shared
between machines (a volume-mounted checkout, say) can only host the demo in one of
them at a time. `demo/clean.sh` removes those directories; run it when switching,
or the other machine's paths surface as a `FileNotFoundError` while generating a
log config.

## If a server fails to start

`demo/start.sh` binds the plain HTTP ports 8080–8082 as well as the HTTPS ones.
If something else already holds one of those — an Element Web dev server, say —
that homeserver dies at startup with `ListenerException`. Stop the other service,
or delete that `- port: 808x` listener from `demo/808x/808x.config` and point the
scripts at the server's HTTPS port instead:

```sh
HS1_CLIENT=https://localhost:8480 demo/msc4430/02-setup-room.sh
```

`HS1_CLIENT`, `HS2_CLIENT` and `HS3_CLIENT` all work that way. Element Web can be
pointed at an HTTPS port too, but you have to open it in a tab and accept the
self-signed certificate first, or its requests fail from inside the app with
nothing useful in the UI.
