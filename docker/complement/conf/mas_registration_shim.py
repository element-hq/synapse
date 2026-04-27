#!/usr/bin/env python3
"""
MAS Registration Shim for Complement testing.

Handles the legacy Matrix registration APIs that MAS doesn't support,
by creating users via the MAS REST Admin API and issuing compatibility
tokens via the mas-cli.

Endpoints:
  GET  /health                             - Healthcheck
  GET  /_synapse/admin/v1/register        - Return a nonce (for shared-secret registration)
  POST /_synapse/admin/v1/register        - Verify HMAC, create user + token
  POST /_matrix/client/v3/register        - m.login.dummy registration
"""

import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

MAS_PORT = int(os.environ.get("MAS_PORT", "8081"))
MAS_BASE = f"http://localhost:{MAS_PORT}"
SHIM_PORT = int(os.environ.get("SHIM_PORT", "8082"))
SERVER_NAME = os.environ.get("SERVER_NAME", "localhost")
REGISTRATION_SHARED_SECRET = os.environ.get("REGISTRATION_SHARED_SECRET", "complement")

# Hardcoded admin client credentials (matches start_for_complement.sh)
MAS_ADMIN_CLIENT_ID = "01HGGCG3PCYWRNJSFQH1RQWQ4N"
MAS_ADMIN_CLIENT_SECRET = "complement-mas-shim-secret"

# In-memory nonce store
_nonces: dict[str, float] = {}


def get_admin_token() -> str:
    """Obtain an admin access token from MAS via client_credentials grant using HTTP Basic Auth."""
    import base64

    token_url = f"{MAS_BASE}/oauth2/token"
    data = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "scope": "urn:mas:admin",
        }
    ).encode()

    creds = base64.b64encode(
        f"{MAS_ADMIN_CLIENT_ID}:{MAS_ADMIN_CLIENT_SECRET}".encode()
    ).decode()

    for attempt in range(30):
        try:
            req = urllib.request.Request(token_url, data=data, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("Authorization", f"Basic {creds}")
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read())
                return body["access_token"]
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            KeyError,
            json.JSONDecodeError,
        ) as e:
            if attempt < 29:
                print(
                    f"[shim] Waiting for MAS token endpoint (attempt {attempt + 1}): {e}"
                )
                time.sleep(1)
            else:
                raise RuntimeError(
                    f"Failed to obtain admin token from MAS after 30 attempts: {e}"
                )
    else:
        raise RuntimeError()


def create_user_in_mas(admin_token: str, username: str) -> str:
    """Create a user via MAS REST Admin API. Returns the user's MAS id (ULID)."""
    url = f"{MAS_BASE}/api/admin/v1/users"
    payload = json.dumps({"username": username}).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {admin_token}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read())
        # MAS returns JSON:API format: {"data": {"id": "...", "attributes": {...}}}
        return body["data"]["id"]


def set_password_in_mas(admin_token: str, user_id: str, password: str) -> None:
    """Set a user's password via MAS REST Admin API."""
    url = f"{MAS_BASE}/api/admin/v1/users/{user_id}/set-password"
    payload = json.dumps({"password": password, "skip_password_check": True}).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {admin_token}")
    with urllib.request.urlopen(req, timeout=10):
        pass  # Expects 204 No Content


def set_admin_in_mas(admin_token: str, user_id: str) -> None:
    """Grant admin privileges to a user via MAS REST Admin API."""
    url = f"{MAS_BASE}/api/admin/v1/users/{user_id}/set-admin"
    payload = json.dumps({"admin": True}).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {admin_token}")
    with urllib.request.urlopen(req, timeout=10):
        pass  # Expects 200 OK


def login_via_mas(username: str, password: str, admin: bool = False) -> dict:
    """Login via MAS compat API to get an access token and device_id."""
    url = f"{MAS_BASE}/_matrix/client/v3/login"
    payload = json.dumps(
        {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": username},
            "password": password,
        }
    ).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read())
    return {
        "user_id": body["user_id"],
        "access_token": body["access_token"],
        "device_id": body["device_id"],
    }


def register_user(username: str, password: str, admin: bool = False) -> dict:
    """Full registration flow: create user, set password, login to get token."""
    admin_token = _get_cached_admin_token()
    user_id = create_user_in_mas(admin_token, username)
    set_password_in_mas(admin_token, user_id, password)
    if admin:
        set_admin_in_mas(admin_token, user_id)
    return login_via_mas(username, password, admin=admin)


class ShimHandler(BaseHTTPRequestHandler):
    """HTTP handler for the registration shim."""

    def log_message(self, fmt: object, *args: object) -> None:
        print(f"[shim] {args[0]}")

    def _send_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length > 0 else b""

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"ok": True})
            return

        if self.path == "/_synapse/admin/v1/register":
            nonce = secrets.token_hex(16)
            _nonces[nonce] = time.time()
            self._send_json(200, {"nonce": nonce})
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        try:
            if self.path == "/_synapse/admin/v1/register":
                self._handle_admin_register()
                return

            if self.path.rstrip("/") == "/_matrix/client/v3/register":
                self._handle_client_register()
                return

            self._send_json(404, {"error": "Not found"})
        except Exception as e:
            print(f"[shim] Error handling {self.path}: {e}")
            self._send_json(500, {"error": str(e)})

    def _handle_admin_register(self) -> None:
        """Handle GET/POST /_synapse/admin/v1/register (shared-secret admin registration)."""
        body = self._read_body()
        data = json.loads(body)

        nonce = data.get("nonce", "")
        if nonce not in _nonces:
            self._send_json(400, {"error": "Unrecognized nonce"})
            return

        # Expire old nonces (5 minutes)
        if time.time() - _nonces[nonce] > 300:
            del _nonces[nonce]
            self._send_json(400, {"error": "Nonce expired"})
            return
        del _nonces[nonce]

        username = data.get("username", "")
        password = data.get("password", "")
        admin = data.get("admin", False)
        mac = data.get("mac", "")

        if not username or not password:
            self._send_json(400, {"error": "username and password are required"})
            return

        # Verify HMAC
        expected_mac = hmac.new(
            REGISTRATION_SHARED_SECRET.encode(),
            f"{nonce}\0{username}\0{password}\0{'admin' if admin else 'notadmin'}".encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(mac, expected_mac):
            self._send_json(403, {"error": "HMAC mismatch"})
            return

        result = register_user(username, password, admin=bool(admin))
        self._send_json(200, result)

    def _handle_client_register(self) -> None:
        """Handle POST /_matrix/client/v3/register (m.login.dummy registration)."""
        body = self._read_body()
        data = json.loads(body)

        auth = data.get("auth", {})
        auth_type = auth.get("type", "")
        username = data.get("username", "")
        password = data.get("password", "")

        if auth_type != "m.login.dummy":
            self._send_json(
                401,
                {
                    "flows": [{"stages": ["m.login.dummy"]}],
                    "params": {},
                    "session": secrets.token_hex(16),
                },
            )
            return

        if not username or not password:
            self._send_json(400, {"error": "username and password are required"})
            return

        result = register_user(username, password, admin=False)
        self._send_json(200, result)


# Cached admin token (obtained lazily on first request)
_admin_token: str | None = None


def _get_cached_admin_token() -> str:
    global _admin_token
    if _admin_token is None:
        _admin_token = get_admin_token()
        print(f"[shim] Obtained admin token: {_admin_token[:8]}...")
    return _admin_token


def main() -> None:
    print(f"[shim] Starting MAS Registration Shim on port {SHIM_PORT}")
    print(f"[shim] MAS endpoint: {MAS_BASE}")
    print(f"[shim] Server name: {SERVER_NAME}")
    print(
        f"[shim] Admin client ID: {MAS_ADMIN_CLIENT_ID[:8]}..."
        if MAS_ADMIN_CLIENT_ID
        else "[shim] WARNING: No admin client ID configured"
    )

    server = HTTPServer(("0.0.0.0", SHIM_PORT), ShimHandler)
    print(f"[shim] Listening on port {SHIM_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
