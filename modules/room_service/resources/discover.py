# discover.py
from synapse.http.server import DirectServeJsonResource

class DiscoverRoomResource(DirectServeJsonResource):
    isLeaf = True

    def __init__(self, api, service):
        super().__init__()
        self.api = api
        self.service = service

    async def _async_render_GET(self, request):
        requester = await self.api.get_user_by_req(request)
        rooms = await self.service.discover(user_id=requester.user.to_string())
        return 200, {"rooms": rooms}
