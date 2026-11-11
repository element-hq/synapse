import pytest
from unittest.mock import MagicMock, AsyncMock

from synapse.api.errors import SynapseError
from modules.room_service.service import RoomService

@pytest.mark.asyncio
async def test_join_by_keyword_room_not_found():
    """
    Deve retornar 404 quando keyword não existe.
    """
    api = MagicMock()
    hs = MagicMock()
    hs.community_gate = None  # community_gate nao carregado
    api._hs = hs

    store = MagicMock()
    store.db_pool.runInteraction = AsyncMock(return_value=None)
    hs.get_datastores.return_value.main = store

    service = RoomService(
        api=api,
        admin_user_id="@admin:localhost",
        admin_token="token",
        homeserver="http://localhost:8008",
    )

    with pytest.raises(SynapseError) as err:
        await service.join_by_keyword(
            target_user_id="@alice:localhost",
            keyword="inexistente",
        )

    assert err.value.code == 404

@pytest.mark.asyncio
async def test_join_by_keyword_success():
    api = MagicMock()
    hs = MagicMock()
    hs.community_gate = None  # community_gate nao carregado
    api._hs = hs

    store = MagicMock()
    store.db_pool.runInteraction = AsyncMock(
        return_value=("!roomid:localhost",)
    )
    hs.get_datastores.return_value.main = store

    agent = MagicMock()
    response = MagicMock()
    response.code = 200

    agent.request = AsyncMock(return_value=response)

    from modules.room_service import service as service_module
    service_module.readBody = AsyncMock(return_value=b"{}")

    service = RoomService(
        api=api,
        admin_user_id="@admin:localhost",
        admin_token="token",
        homeserver="http://localhost:8008",
    )

    service.agent = agent

    await service.join_by_keyword(
        target_user_id="@alice:localhost",
        keyword="buzz",
    )

    agent.request.assert_awaited_once()

@pytest.mark.asyncio
async def test_join_by_keyword_admin_join_fails():
    """
    Deve retornar erro quando endpoint admin join falha.
    """
    api = MagicMock()
    hs = MagicMock()
    hs.community_gate = None  # community_gate nao carregado
    api._hs = hs

    store = MagicMock()
    store.db_pool.runInteraction = AsyncMock(
        return_value=("!roomid:localhost",)
    )
    hs.get_datastores.return_value.main = store

    agent = MagicMock()
    response = MagicMock()
    response.code = 500

    agent.request = AsyncMock(return_value=response)

    from modules.room_service import service as service_module
    service_module.readBody = AsyncMock(return_value=b"boom")

    service = RoomService(
        api=api,
        admin_user_id="@admin:localhost",
        admin_token="token",
        homeserver="http://localhost:8008",
    )

    service.agent = agent

    with pytest.raises(SynapseError) as err:
        await service.join_by_keyword(
            target_user_id="@alice:localhost",
            keyword="buzz",
        )

    assert err.value.code == 500
