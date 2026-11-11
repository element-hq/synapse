import pytest
from unittest.mock import AsyncMock, MagicMock

from twisted.web.test.requesthelper import DummyRequest

from modules.room_service.resources.discover import DiscoverRoomResource

@pytest.mark.asyncio
async def test_discover_success():
    """
    Deve retornar 200 e lista de salas corretamente formatada.
    """
    api = MagicMock()
    requester = MagicMock()
    requester.user.to_string.return_value = "@alice:localhost"
    api.get_user_by_req = AsyncMock(return_value=requester)

    service = MagicMock()
    service.discover = AsyncMock(return_value=[
        {
            "room_id": "!room:localhost",
            "name": "Sala Teste",
            "room_kind": "group",
            "access_type": "free",
            "price": 0,
            "keyword": "buzz",
            "member_count": 3,
        }
    ])

    resource = DiscoverRoomResource(api, service)

    request = DummyRequest([b"_synapse", b"room_service", b"discover"])

    code, body = await resource._async_render_GET(request)

    assert code == 200
    assert "rooms" in body
    assert len(body["rooms"]) == 1

    room = body["rooms"][0]
    assert room["name"] == "Sala Teste"
    assert room["member_count"] == 3

    service.discover.assert_called_once_with(user_id="@alice:localhost")
