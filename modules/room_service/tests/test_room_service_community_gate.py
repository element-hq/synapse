import json
import pytest
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock

from twisted.web.test.requesthelper import DummyRequest

from synapse.api.errors import SynapseError

from modules.room_service.service import RoomService
from modules.room_service.resources.discover import DiscoverRoomResource

# Integracao do room_service com o community_gate (api._hs.community_gate):
# 1. discover esconde o que nao e de comunidade de quem e da comunidade
# 2. discover sem gate carregado continua igual
# 3. join_by_keyword e invite_space respondem 403 quando o gate nega,
#    sem chamar o admin join
# 4. o endpoint de discover exige login e repassa o usuario

ADMIN = "@admin:localhost"
FAN = "@fan:localhost"
STAFF = "@staff:localhost"
INTERNAL_ROOM = "!geral:localhost"
COMMUNITY_ROOM = "!victor:localhost"


class FakeGate:
    def __init__(self, internal=(STAFF, ADMIN), community_rooms=(COMMUNITY_ROOM,)):
        self.internal = set(internal)
        self.community_rooms = set(community_rooms)

    async def is_internal(self, user_id):
        return user_id in self.internal

    async def is_community_room(self, room_id):
        return room_id in self.community_rooms

    async def may_join(self, user_id, room_id):
        return user_id in self.internal or room_id in self.community_rooms


def _service(gate=None, rows=None):
    api = MagicMock()
    hs = MagicMock(spec=["get_room_member_handler", "get_datastores", "get_reactor"])
    if gate is not None:
        hs = MagicMock()
        hs.community_gate = gate
    api._hs = hs

    store = MagicMock()
    store.get_users_in_room = AsyncMock(return_value=[ADMIN, "u1"])
    store.db_pool.runInteraction = AsyncMock(
        return_value=rows
        if rows is not None
        else [
            (INTERNAL_ROOM, "group", "public", 0, "geral"),
            (COMMUNITY_ROOM, "space", "private", 0, "victor"),
        ]
    )
    hs.get_datastores.return_value.main = store
    api.get_state_events_in_room = AsyncMock(return_value=[MagicMock(content={"name": "Sala"})])

    service = RoomService(
        api=api,
        admin_user_id=ADMIN,
        admin_token="token",
        homeserver="http://localhost:8008",
    )
    service._admin_join = AsyncMock(return_value="joined")
    service.agent = MagicMock()
    service.agent.request = AsyncMock()
    return service


# ---------------- discover ----------------


@pytest.mark.asyncio
async def test_discover_esconde_salas_internas_da_comunidade():
    service = _service(gate=FakeGate())
    rooms = await service.discover(user_id=FAN)
    assert [r["room_id"] for r in rooms] == [COMMUNITY_ROOM]


@pytest.mark.asyncio
async def test_discover_mostra_tudo_para_interno():
    service = _service(gate=FakeGate())
    rooms = await service.discover(user_id=STAFF)
    assert [r["room_id"] for r in rooms] == [INTERNAL_ROOM, COMMUNITY_ROOM]


@pytest.mark.asyncio
async def test_discover_sem_gate_carregado_mostra_tudo():
    service = _service(gate=None)
    rooms = await service.discover(user_id=FAN)
    assert [r["room_id"] for r in rooms] == [INTERNAL_ROOM, COMMUNITY_ROOM]


# ---------------- join_by_keyword ----------------


@pytest.mark.asyncio
async def test_join_by_keyword_negado_pelo_gate_responde_403():
    service = _service(gate=FakeGate())
    service.store.db_pool.runInteraction = AsyncMock(return_value=(INTERNAL_ROOM,))

    with pytest.raises(SynapseError) as exc:
        await service.join_by_keyword(target_user_id=FAN, keyword="geral")

    assert exc.value.code == 403
    service.agent.request.assert_not_awaited()


# ---------------- invite_space ----------------


@pytest.mark.asyncio
async def test_invite_space_negado_pelo_gate_responde_403():
    service = _service(gate=FakeGate())
    service._get_space_children = AsyncMock(return_value=["!filha:localhost"])

    with pytest.raises(SynapseError) as exc:
        await service.invite_space(user_id=FAN, space_id=INTERNAL_ROOM)

    assert exc.value.code == 403
    service._admin_join.assert_not_awaited()


@pytest.mark.asyncio
async def test_invite_space_permitido_pelo_gate_segue_normal():
    service = _service(gate=FakeGate())
    service._get_space_children = AsyncMock(return_value=["!filha:localhost"])

    joined = await service.invite_space(user_id=FAN, space_id=COMMUNITY_ROOM)

    assert joined == ["!filha:localhost"]


# ---------------- endpoint discover ----------------


@pytest.mark.asyncio
async def test_endpoint_discover_exige_login_e_repassa_usuario():
    api = MagicMock()
    requester = MagicMock()
    requester.user.to_string.return_value = FAN
    api.get_user_by_req = AsyncMock(return_value=requester)
    service = MagicMock()
    service.discover = AsyncMock(return_value=[])

    request = DummyRequest([b"_synapse", b"room_service", b"discover"])
    request.method = b"GET"
    request.content = BytesIO(json.dumps({}).encode())

    code, body = await DiscoverRoomResource(api, service)._async_render_GET(request)

    assert code == 200
    api.get_user_by_req.assert_awaited_once_with(request)
    service.discover.assert_awaited_once_with(user_id=FAN)
