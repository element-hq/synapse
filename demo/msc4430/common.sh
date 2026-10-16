#!/usr/bin/env bash
# Shared settings and narration helpers for the MSC4430 account portability demo.
#
# The three homeservers are the ones `demo/start.sh` makes. Its server names are
# the *HTTPS* ports, while clients talk to the plain HTTP ports, so each server
# has two numbers and it is easy to mix them up:
#
#   | role in this demo        | client API             | server name     | database       |
#   |--------------------------|------------------------|-----------------|----------------|
#   | ALICE's first server     | http://localhost:8080  | localhost:8480  | demo/8080/*.db |
#   | BOB's server (bystander) | http://localhost:8081  | localhost:8481  | demo/8081/*.db |
#   | ALICE's second server    | http://localhost:8082  | localhost:8482  | demo/8082/*.db |
#
# The client API is plain HTTP. Only federation needs TLS here, because a server
# name has to match the port its federation listener is on, and demo/start.sh
# serves `client` on both ports anyway. Set HS1_CLIENT / HS2_CLIENT / HS3_CLIENT
# in the environment to override — the HTTPS ports (https://localhost:848x) work
# too, if something else is already using a plain port.

set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYNAPSE_DIR="$(cd "$DEMO_DIR/.." && pwd)"
STATE_FILE="$DEMO_DIR/msc4430/state.env"

HS1_CLIENT="${HS1_CLIENT:-http://localhost:8080}"; HS1_NAME="localhost:8480"; HS1_DB="$DEMO_DIR/8080/homeserver.db"
HS2_CLIENT="${HS2_CLIENT:-http://localhost:8081}"; HS2_NAME="localhost:8481"; HS2_DB="$DEMO_DIR/8081/homeserver.db"
HS3_CLIENT="${HS3_CLIENT:-http://localhost:8082}"; HS3_NAME="localhost:8482"; HS3_DB="$DEMO_DIR/8082/homeserver.db"

ROOM_VERSION="org.matrix.msc4430.12"
DEMO_PASSWORD="demo-password"
PYTHON="${PYTHON:-python3}"

# This sandbox sets HTTP_PROXY globally, which would send requests for localhost
# to a proxy that cannot reach it.
export no_proxy="*" NO_PROXY="*"
# --insecure matters only if these are pointed at the HTTPS ports, whose
# certificates demo/start.sh self-signs.
CURL=(curl --silent --show-error --noproxy '*' --insecure)

# --- narration ----------------------------------------------------------
# Every step says what it is about to do before doing it: this demo is mostly
# interesting for *why* each step works, and a script that silently succeeds
# teaches nobody anything.

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
explain() { printf '\033[2m%s\033[0m\n' "$*"; }
run() { printf '\033[1;33m$ %s\033[0m\n' "$*"; "$@"; }
note() { printf '\033[1;35m-> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;31m!! %s\033[0m\n' "$*"; }

confirm() {
    if [ "${ASSUME_YES:-}" = "1" ]; then
        note "(--yes given, continuing without asking)"
        return 0
    fi
    read -r -p "$(printf '\033[1;31m?? %s [y/N] \033[0m' "$1")" reply
    case "$reply" in [yY]*) return 0 ;; *) echo "Stopping here."; exit 1 ;; esac
}

sql() { sqlite3 "$1" "$2"; }

register() {
    # $1 client base URL, $2 localpart, $3 password. Prints an access token.
    "${CURL[@]}" -XPOST "$1/_matrix/client/v3/register" \
        -d "{\"username\":\"$2\",\"password\":\"$3\",\"auth\":{\"type\":\"m.login.dummy\"},\"inhibit_login\":false}" \
    | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
}

login() {
    "${CURL[@]}" -XPOST "$1/_matrix/client/v3/login" \
        -d "{\"type\":\"m.login.password\",\"identifier\":{\"type\":\"m.id.user\",\"user\":\"$2\"},\"password\":\"$3\"}" \
    | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
}

print_logins() {
    # $1: the room ID. Everything a client needs to take over from here.
    say "Driving it from a client instead"
    explain "Element Web asks for a homeserver URL and then a username. These are"
    explain "plain HTTP, so there is no certificate to accept first; the servers"
    explain "only use TLS between themselves."
    printf '\n'
    printf '  \033[1mAlice, on her first server\033[0m   (the account that moves away)\n'
    printf '    homeserver  %s\n' "$HS1_CLIENT"
    printf '    username    alice            password  %s\n' "$DEMO_PASSWORD"
    printf '    user ID     @alice:%s\n\n' "$HS1_NAME"
    printf '  \033[1mBob\033[0m                           (the bystander: watch this one)\n'
    printf '    homeserver  %s\n' "$HS2_CLIENT"
    printf '    username    bob              password  %s\n' "$DEMO_PASSWORD"
    printf '    user ID     @bob:%s\n\n' "$HS2_NAME"
    printf '  \033[1mAlice, on her second server\033[0m  (the account she moves to)\n'
    printf '    homeserver  %s\n' "$HS3_CLIENT"
    printf '    username    alice            password  %s\n' "$DEMO_PASSWORD"
    printf '    user ID     @alice:%s\n\n' "$HS3_NAME"
    printf '  \033[1mRoom\033[0m  %s\n\n' "$1"
    explain "Bob is the view worth having open while you run 03 and 04: his client"
    explain "is told who Alice is by MSC4428's member_info map, so her user ID in his"
    explain "member list changes from @alice:$HS1_NAME to"
    explain "@alice:$HS3_NAME without her leaving the room or"
    explain "sending anything he can see. Alice's first client will carry on showing"
    explain "the room as it was: its server is no longer being sent anything."
    explain ""
    explain "Alice's second account is not in the room yet — 04-rejoin.sh is what"
    explain "puts it there. Log in and leave it on the room list; the room appears"
    explain "when the move completes."
}

require_synapse_python() {
    # demo/start.sh runs `python3 -m synapse.app.homeserver`, so python3 has to be
    # the one Synapse is installed into. Without this the failure is a traceback
    # about a missing dependency, which says nothing about the real problem.
    if ! "$PYTHON" -c 'import synapse' >/dev/null 2>&1; then
        warn "\"$PYTHON\" cannot import synapse."
        warn "Activate the project virtualenv first, so that this and the servers"
        warn "demo/start.sh launches both use it:"
        warn ""
        warn "    source \"\$(poetry env info --path)/bin/activate\""
        warn ""
        warn "(or set PYTHON=/path/to/venv/bin/python for these scripts alone, which"
        warn "will not help demo/start.sh)."
        exit 1
    fi
}

require_state() {
    if [ ! -f "$STATE_FILE" ]; then
        warn "No $STATE_FILE — run demo/msc4430/02-setup-room.sh first."
        exit 1
    fi
    # shellcheck disable=SC1090
    . "$STATE_FILE"
}
