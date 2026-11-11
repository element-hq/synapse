import pytest
from unittest.mock import AsyncMock, MagicMock

from synapse.api.errors import SynapseError
from modules.bundle_service.service import BundleService


@pytest.mark.asyncio
async def test_invite_bundle_success():
    """
    Deve adicionar o usuário em todas as rooms do bundle.
    """

    api = MagicMock()

    store = MagicMock()
    db_pool = MagicMock()

    api._hs.get_datastores.return_value.main = store
    store.db_pool = db_pool

    room_service = MagicMock()
    room_service._community_gate = MagicMock(return_value=None)  # gate nao carregado
    room_service._admin_join = AsyncMock()

    service = BundleService(api, room_service)

    db_pool.runInteraction = AsyncMock(return_value=[
        ("!room1:localhost",),
        ("!room2:localhost",),
    ])

    result = await service.invite_bundle(
        "@user:localhost",
        "bundle1",
    )

    assert len(result) == 2
    assert "!room1:localhost" in result
    assert "!room2:localhost" in result
    assert room_service._admin_join.await_count == 2


@pytest.mark.asyncio
async def test_invite_bundle_no_rooms():
    """
    Deve retornar erro se o bundle não tiver rooms.
    """

    api = MagicMock()

    store = MagicMock()
    db_pool = MagicMock()

    api._hs.get_datastores.return_value.main = store
    store.db_pool = db_pool

    room_service = MagicMock()
    room_service._community_gate = MagicMock(return_value=None)  # gate nao carregado
    room_service._admin_join = AsyncMock()

    service = BundleService(api, room_service)

    db_pool.runInteraction = AsyncMock(return_value=[])

    with pytest.raises(SynapseError) as err:
        await service.invite_bundle(
            "@user:localhost",
            "bundle1",
        )

    assert err.value.code == 404


@pytest.mark.asyncio
async def test_invite_bundle_partial_failure():
    """
    Deve continuar o fluxo mesmo se falhar ao entrar em uma das rooms.
    """

    api = MagicMock()

    store = MagicMock()
    db_pool = MagicMock()

    api._hs.get_datastores.return_value.main = store
    store.db_pool = db_pool

    room_service = MagicMock()
    room_service._community_gate = MagicMock(return_value=None)  # gate nao carregado

    async def mock_join(room_id, user_id):
        if room_id == "!room2:localhost":
            raise Exception("join failed")

    room_service._admin_join = AsyncMock(side_effect=mock_join)

    service = BundleService(api, room_service)

    db_pool.runInteraction = AsyncMock(return_value=[
        ("!room1:localhost",),
        ("!room2:localhost",),
    ])

    result = await service.invite_bundle(
        "@user:localhost",
        "bundle1",
    )

    assert len(result) == 1
    assert "!room1:localhost" in result
    assert "!room2:localhost" not in result


@pytest.mark.asyncio
async def test_invite_bundle_string_rows():
    """
    Deve funcionar quando o DB retorna lista de strings (sem tuple).
    """

    api = MagicMock()

    store = MagicMock()
    db_pool = MagicMock()

    api._hs.get_datastores.return_value.main = store
    store.db_pool = db_pool

    room_service = MagicMock()
    room_service._community_gate = MagicMock(return_value=None)  # gate nao carregado
    room_service._admin_join = AsyncMock()

    service = BundleService(api, room_service)

    db_pool.runInteraction = AsyncMock(return_value=[
        "!room1:localhost",
        "!room2:localhost",
    ])

    result = await service.invite_bundle(
        "@user:localhost",
        "bundle1",
    )

    assert len(result) == 2
    assert room_service._admin_join.await_count == 2

@pytest.mark.asyncio
async def test_invite_bundle_without_room_service():
    """
    Deve retornar erro se o room_service não estiver configurado.
    """

    api = MagicMock()

    store = MagicMock()
    db_pool = MagicMock()

    api._hs.get_datastores.return_value.main = store
    store.db_pool = db_pool

    # NÃO passa room_service
    service = BundleService(api)

    db_pool.runInteraction = AsyncMock(return_value=[
        ("!room1:localhost",),
    ])

    with pytest.raises(SynapseError) as err:
        await service.invite_bundle(
            "@user:localhost",
            "bundle1",
        )

    assert err.value.code == 500