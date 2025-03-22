import secrets
import ssl
from dataclasses import dataclass, field

from aiohttp import web
from signedjson.key import decode_signing_key_base64
from signedjson.types import SigningKey

from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.crypto.event_signing import compute_event_signature

routes = web.RouteTableDef()

JOIN_FLOW_PAGE = """
<html>
<body>
<a href="/accept?redirect_url={redirect_url}" target="_self">Accept policy and join room</a>
</body>
</html>
"""


SIGNING_KEY = decode_signing_key_base64(
    "ed25519", "p_afG2", "E+EmxfcqLYjlS20I5ZzjoYeN7oR9Qt/zitPGomU0hmA"
)


@dataclass
class PolicyServer:
    server_name: str
    signing_key: SigningKey
    base_url: str
    token_store: dict[str, str] = field(default_factory=dict)


@routes.get("/")
async def hello(request):
    return web.Response(text="Hello, world")


@routes.post("/_matrix/federation/unstable/re.jki.join_policy/request_join")
async def request_join(request: web.Request) -> web.Response:
    policy_server: PolicyServer = request.app["policy_server"]
    return web.json_response({"url": policy_server.base_url + "/join_flow"})


@routes.post("/_matrix/federation/unstable/re.jki.join_policy/sign_join")
async def sign_join(request: web.Request) -> web.Response:
    policy_server: PolicyServer = request.app["policy_server"]

    json_body = await request.json()
    if json_body["token"] not in policy_server.token_store:
        return web.json_response({}, status=403)

    room_version_id = json_body["room_version"]
    event_json = json_body["event"]

    room_version = KNOWN_ROOM_VERSIONS[room_version_id]

    signatures = compute_event_signature(
        room_version=room_version,
        event_dict=event_json,
        signature_name=policy_server.server_name,
        signing_key=policy_server.signing_key,
    )

    return web.json_response({"signatures": signatures[policy_server.server_name]})


@routes.get("/join_flow")
async def join_flow(request: web.Request) -> web.Response:
    redirect_url = request.query["redirect_url"]
    return web.Response(
        text=JOIN_FLOW_PAGE.format(redirect_url=redirect_url), content_type="text/html"
    )


@routes.get("/accept")
async def accept(request: web.Request) -> web.Response:
    policy_server: PolicyServer = request.app["policy_server"]

    redirect_url = request.query["redirect_url"]

    token = secrets.token_hex(16)
    policy_server.token_store[token] = "user_id"

    # TODO: Use less dodgy URL creation
    if "?" in redirect_url:
        redirect_url += f"&token={token}"
    else:
        redirect_url += f"?token={token}"

    return web.Response(
        text="Done!",
        status=307,
        headers={"location": redirect_url},
    )


context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
context.load_cert_chain(
    certfile="/home/erikj/git/synapse/demo/8080/localhost:8080.tls.crt",
    keyfile="/home/erikj/git/synapse/demo/8080/localhost:8080.tls.key",
)


app = web.Application()
app["policy_server"] = PolicyServer(
    server_name="localhost:8865",
    signing_key=SIGNING_KEY,
    base_url="https://localhost:8865",
)
app.add_routes(routes)
web.run_app(app, port=8865, ssl_context=context)
