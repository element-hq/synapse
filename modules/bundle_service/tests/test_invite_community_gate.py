import pytest
from unittest.mock import AsyncMock, MagicMock

from modules.bundle_service.service import BundleService

# invite_bundle com o community_gate carregado: sala que o gate nega nem
# chega ao admin join (que convidaria antes do join ser barrado, deixando
# um convite pendurado com o nome da sala interna).

FAN = "@fan:localhost"
INTERNAL_ROOM = "!geral:localhost"
COMMUNITY_ROOM = "!victor:localhost"


class FakeGate:
    async def may_join(self, user_id, room_id):
        return room_id == COMMUNITY_ROOM


def _service(gate):
    api = MagicMock()
    store = MagicMock()
    store.db_pool.runInteraction = AsyncMock(
        return_value=[(INTERNAL_ROOM,), (COMMUNITY_ROOM,)]
    )
    api._hs.get_datastores.return_value.main = store

    room_service = MagicMock()
    room_service._admin_join = AsyncMock(return_value="joined")
    room_service._community_gate = MagicMock(return_value=gate)
    return BundleService(api, room_service=room_service), room_service


@pytest.mark.asyncio
async def test_invite_bundle_pula_sala_negada_pelo_gate():
    service, room_service = _service(FakeGate())

    joined = await service.invite_bundle(user_id=FAN, bundle_id="b1")

    assert joined == [COMMUNITY_ROOM]
    room_service._admin_join.assert_awaited_once_with(COMMUNITY_ROOM, FAN)


@pytest.mark.asyncio
async def test_invite_bundle_sem_gate_entra_em_todas():
    service, room_service = _service(None)

    joined = await service.invite_bundle(user_id=FAN, bundle_id="b1")

    assert joined == [INTERNAL_ROOM, COMMUNITY_ROOM]
