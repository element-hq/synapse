# service.py
import logging
import json
from urllib.parse import quote

from synapse.module_api import ModuleApi
from synapse.api.errors import SynapseError

from twisted.web.client import Agent, readBody
from twisted.web.http_headers import Headers
from twisted.web.iweb import IBodyProducer
from twisted.internet.defer import succeed
from zope.interface import implementer

from . import db

logger = logging.getLogger(__name__)


@implementer(IBodyProducer)
class _BodyProducer:
    def __init__(self, body: bytes):
        self.body = body
        self.length = len(body)

    def startProducing(self, consumer):
        consumer.write(self.body)
        return succeed(None)

    def pauseProducing(self):
        pass

    def stopProducing(self):
        pass


class RoomService:
    def __init__(
        self,
        api: ModuleApi,
        admin_user_id: str,
        admin_token: str,
        homeserver: str,
    ):
        self.api = api
        self.hs = api._hs
        self.admin_user_id = admin_user_id
        self.admin_token = admin_token
        self.homeserver = homeserver
        self.room_member_handler = self.hs.get_room_member_handler()

        self.store = self.hs.get_datastores().main
        self.agent = Agent(self.hs.get_reactor())

    # ---------------- DISCOVER ----------------
    async def discover(self, user_id: str | None = None):
        logger.info("discover: start fetching visible rooms")
        rows = await self.store.db_pool.runInteraction(
            "get_visible_rooms",
            db.get_visible_rooms,
        )

        logger.info("discover: %d visible rooms found in DB", len(rows))

        # Comunidade externa so enxerga as salas da propria comunidade
        # (community_gate). Sem gate carregado, nada muda.
        gate = self._community_gate()
        if gate is not None and user_id and not await gate.is_internal(user_id):
            rows = [row for row in rows if await gate.is_community_room(row[0])]
        rooms = []

        for room_id, room_kind, access_type, price, keyword in rows:
            logger.debug(
                "discover: processing room_id=%s kind=%s access=%s visible_price=%s keyword=%s",
                room_id,
                room_kind,
                access_type,
                price,
                keyword,
            )
            if not await self._is_user_in_room(room_id, self.admin_user_id):
                logger.debug(
                    "discover: skipping room_id=%s (admin not in room)",
                    room_id,
                )
                continue

            state_events = await self.api.get_state_events_in_room(
                room_id,
                [("m.room.name", "")]
            )

            name = "Sem nome"
            for ev in state_events:
                name = ev.content.get("name", "Sem nome")
                break

            users = await self.store.get_users_in_room(room_id)
            member_count = len(users)

            logger.debug(
                "discover: room_id=%s name='%s' members=%d",
                room_id,
                name,
                member_count,
            )
            rooms.append({
                "room_id": room_id,
                "name": name,
                "room_kind": room_kind,
                "access_type": access_type,
                "price": price,
                "keyword": keyword,
                "member_count": member_count,
            })

        logger.info(
            "discover: finished, %d rooms returned",
            len(rooms),
        )

        return rooms

    # ---------------- INVITE ----------------
    async def join_by_keyword(self, target_user_id: str, keyword: str):
        logger.info(
            "join_by_keyword: start target=%s keyword=%s",
            target_user_id,
            keyword,
        )
        row = await self.store.db_pool.runInteraction(
            "get_room_by_keyword",
            db.get_room_by_keyword,
            keyword,
        )
        
        if not row:
            logger.warning(
                "join_by_keyword: no room found for keyword=%s",
                keyword,
            )
            raise SynapseError(404, "Room not found")

        room_id = row[0]

        await self._assert_gate_allows_join(target_user_id, room_id)

        logger.info(
            "join_by_keyword: room_id=%s target=%s",
            room_id,
            target_user_id,
        )

        url = (
            f"{self.homeserver}/_synapse/admin/v1/join/"
            f"{quote(room_id)}"
        )

        payload = json.dumps({"user_id": target_user_id}).encode()

        logger.debug(
            "join_by_keyword: sending admin join request user=%s room_id=%s",
            target_user_id,
            room_id,
        )
        response = await self.agent.request(
            b"POST",
            url.encode(),
            Headers({
                b"Authorization": [f"Bearer {self.admin_token}".encode()],
                b"Content-Type": [b"application/json"],
            }),
            bodyProducer=_BodyProducer(payload),
        )

        response_body = await readBody(response)

        if response.code != 200:
            logger.error(
                "admin join failed code=%s body=%s",
                response.code,
                response_body.decode(errors="ignore"),
            )
            raise SynapseError(500, "admin join failed")

        logger.info(
            "join_by_keyword: success user=%s room=%s",
            target_user_id,
            room_id,
        )
    
    # ---------------- CREATE ----------------
    async def create_room(self, requester, data):
        creator = requester.user.to_string()
        logger.info(
            "create_room: start creator=%s data_keys=%s",
            creator,
            list(data.keys()),
        )
        await self.assert_is_admin(creator)

        if not data.get("keyword"):
            logger.warning(
                "create_room: missing keyword creator=%s",
                creator,
            )
            raise SynapseError(400, "Keyword is required")

        if not data.get("name"):
            logger.warning(
                "create_room: missing name creator=%s",
                creator,
            )
            raise SynapseError(400, "Name is required")
        
        exists = await self.store.db_pool.runInteraction(
            "keyword_exists",
            db.keyword_exists,
            data["keyword"],
        )

        if exists:
            logger.warning(
                "create_room: keyword already in use keyword=%s creator=%s",
                data["keyword"],
                creator,
            )
            raise SynapseError(409, "Keyword already in use")

        visible = bool(data.get("visible", False))
        access_type = data.get("access_type", "private")

        price = int(data.get("price", 0))
        if not visible:
            price = 0

        logger.debug(
            "create_room: business params keyword=%s visible=%s access_type=%s price=%s",
            data["keyword"],
            visible,
            access_type,
            price,
        )

        join_rule = "public"

        # room_kind decide se criamos um group (room normal) ou um space.
        # get_visible_rooms ja aceita ('group', 'space'), entao um space
        # visivel aparece no discover pelo mesmo caminho da room.
        room_kind = data.get("room_kind", "group")
        is_space = room_kind == "space"

        room_config = {
            "name": data["name"],
            "is_direct": False,
            "visibility": "private",
            "initial_state": [
                {
                    "type": "m.room.join_rules",
                    "state_key": "",
                    "content": {"join_rule": join_rule},
                },
                {
                    "type": "m.room.power_levels",
                    "state_key": "",
                    "content": {
                        "users": {
                            creator: 100,
                            self.admin_user_id: 100,
                        },
                        "users_default": 0,
                        "events_default": 50,
                        "state_default": 50,
                        "ban": 50,
                        "kick": 50,
                        "redact": 50,
                        "invite": 50,
                    },
                },
            ],
        }
        # marca o room como space na criacao (o que o check_event_allowed
        # barrava antes). Sem isso o Matrix cria uma room normal.
        if is_space:
            room_config["creation_content"] = {"type": "m.space"}

        logger.info(
            "create_room: creating %s name='%s' creator=%s",
            "space" if is_space else "room",
            data.get("name"),
            creator,
        )
        room_id, _ = await self.api.create_room(
            user_id=creator,
            config=room_config,
        )

        logger.info(
            "create_room: room created room_id=%s",
            room_id,
        )

        await self.store.db_pool.runInteraction(
            "save_room_metadata",
            db.save_room_metadata,
            room_id,
            {
                **data,
                "room_kind": room_kind,
                "access_type": access_type,
                "price": price,
                "visible": visible,
            },
        )   

        logger.debug(
            "create_room: metadata saved room_id=%s",
            room_id,
        )

        await self._admin_join(room_id, self.admin_user_id)

        logger.debug(
            "create_room: admin joined room room_id=%s admin=%s",
            room_id,
            self.admin_user_id,
        )

        if access_type == "private":
            logger.info(
                "create_room: configuring private access room_id=%s",
                room_id,
            )
            await self._admin_send_state(
                room_id,
                "m.room.join_rules",
                "",
                {"join_rule": "invite"},
            )
            await self._admin_send_state(
                room_id,
                "m.room.guest_access",
                "",
                {"guest_access": "forbidden"},
            )

        logger.info(
            "create_room: success room_id=%s creator=%s",
            room_id,
            creator,
        )
        return room_id

    # ---------------- CHANGE VISIBILITY ----------------
    async def change_visibility(
        self,
        *,
        requester,
        room_id: str,
        visible: bool,
        price: int | None = None,
    ):
        logger.info(
            "change_visibility: start room_id=%s requester=%s visible=%s price=%s",
            room_id,
            requester.user.to_string(),
            visible,
            price,
        )
        user_id = requester.user.to_string()
        await self.assert_is_admin(user_id)

        access_type = await self.store.db_pool.runInteraction(
            "get_room_access_type",
            db.get_room_access_type,
            room_id,
        )

        if access_type is None:
            logger.warning(
                "change_visibility: room not found room_id=%s",
                room_id,
            )
            raise SynapseError(404, "Room not found")

        access_type = access_type.lower()

        logger.debug(
            "change_visibility: current access_type=%s room_id=%s",
            access_type,
            room_id,
        )

        if not visible:
            logger.debug(
                "change_visibility: setting invisible, price forced to 0 room_id=%s",
                room_id,
            )
            price = 0

        else:
            if access_type == "private":
                if price is None or price <= 0:
                    logger.warning(
                        "change_visibility: missing/invalid price for private room "
                        "room_id=%s price=%s",
                        room_id,
                        price,
                    )
                    raise SynapseError(
                        400,
                        "Missing price: private visible rooms must define a price",
                    )
            else:
                # public
                logger.debug(
                    "change_visibility: public room, price forced to 0 room_id=%s",
                    room_id,
                )
                price = 0

        logger.info(
            "change_visibility: updating DB room_id=%s visible=%s price=%s",
            room_id,
            visible,
            price,
        )

        await self.store.db_pool.runInteraction(
            "update_room_visibility",
            db.update_room_visibility,
            room_id,
            visible,
            price,
        )

        logger.info(
            "change_visibility: success room_id=%s visible=%s price=%s",
            room_id,
            visible,
            price,
        )

        return {
            "room_id": room_id,
            "visible": visible,
            "price": price,
        }

    # ---------------- GET VISIBILITY ----------------
    async def get_room_visibility(self, *, room_id: str):
        logger.info(
            "get_room_visibility: start room_id=%s",
            room_id,
        )

        row = await self.store.db_pool.runInteraction(
            "get_room_visibility",
            db.get_room_visibility,
            room_id,
        )

        if not row:
            logger.warning(
                "get_room_visibility: room not found room_id=%s",
                room_id,
            )
            raise SynapseError(404, "Room not found")

        visible, price, access_type = row

        logger.info(
            "get_room_visibility: success room_id=%s visible=%s price=%s access_type=%s",
            room_id,
            bool(visible),
            price,
            access_type,
        )

        return {
            "room_id": room_id,
            "visible": bool(visible),
            "price": int(price),
            "access_type": access_type,
        }

    # ---------------- CHANGE PRICE ----------------
    async def change_price(
        self,
        *,
        requester,
        room_id: str,
        price: int,
    ):
        user_id = requester.user.to_string()
        logger.info(
            "change_price: start user=%s room_id=%s requested_price=%s",
            user_id,
            room_id,
            price,
        )

        await self.assert_is_admin(user_id)

        row = await self.store.db_pool.runInteraction(
            "get_room_price_info",
            db.get_room_price_info,
            room_id,
        )

        if not row:
            logger.warning(
                "change_price: room not found room_id=%s user=%s",
                room_id,
                user_id,
            )
            raise SynapseError(404, "Room not found")

        visible_raw, access_type, current_price = row
        visible = bool(visible_raw)

        logger.debug(
            "change_price: current state room_id=%s visible=%s access_type=%s current_price=%s",
            room_id,
            visible,
            access_type,
            current_price,
        )
        if not visible:
            if price not in (None, 0):
                logger.warning(
                    "change_price: rejected non-visible room room_id=%s price=%s",
                    room_id,
                    price,
                )
                raise SynapseError(
                    400,
                    "Cannot set price on a non-visible room",
                )
            price = 0

        else:
            if access_type == "private":
                if price is None or price <= 0:
                    logger.warning(
                        "change_price: invalid price for private room "
                        "room_id=%s price=%s",
                        room_id,
                        price,
                    )
                    raise SynapseError(
                        400,
                        "Private visible rooms must have price > 0",
                    )
            else:
                if price not in (None, 0):
                    logger.warning(
                        "change_price: rejected price on public room "
                        "room_id=%s price=%s",
                        room_id,
                        price,
                    )
                    raise SynapseError(
                        400,
                        "Public rooms cannot have a price",
                    )
                price = 0
        logger.info(
            "change_price: updating price room_id=%s new_price=%s",
            room_id,
            price,
        )
        await self.store.db_pool.runInteraction(
            "update_room_price",
            db.update_room_price,
            room_id,
            price,
        )

        logger.info(
            "change_price: success room_id=%s price=%s",
            room_id,
            price,
        )
        return {
            "room_id": room_id,
            "price": price,
        }

    # ---------------- CHANGE ACCESS TYPE ---------------
    async def change_access_type(
        self,
        *,
        requester,
        room_id: str,
        access_type: str,
        price: int | None = None,
    ):
        logger.info(
            "change_access_type: start room_id=%s user=%s access_type=%s price=%s",
            room_id,
            requester.user.to_string(),
            access_type,
            price,
        )

        await self.assert_is_admin(requester.user.to_string())

        access_type = access_type.lower()
        if access_type not in ("public", "private"):
            raise SynapseError(400, "Invalid access_type")

        row = await self.store.db_pool.runInteraction(
            "get_room_price_info",
            db.get_room_price_info,
            room_id,
        )

        if not row:
            raise SynapseError(404, "Room not found")

        visible_raw, current_access_type, current_price = row
        visible = bool(visible_raw)

        logger.debug(
            "change_access_type: current state room_id=%s visible=%s access_type=%s price=%s",
            room_id,
            visible,
            current_access_type,
            current_price,
        )

        if not visible:
            logger.debug(
                "change_access_type: room not visible, forcing price=0 room_id=%s",
                room_id,
            )
            price = 0

        else:
            if current_access_type == access_type:
                price = current_price
            elif current_access_type == "public" and access_type == "private":
                # PUBLIC → PRIVATE (visível)
                if price is None or price <= 0:
                    price = 1000
                    logger.info(
                        "change_access_type: no price provided, applying default price=%s room_id=%s",
                        price,
                        room_id,
                    )
                    
            elif current_access_type == "private" and access_type == "public":
                # PRIVATE → PUBLIC
                logger.debug(
                    "change_access_type: private->public, forcing price=0 room_id=%s",
                    room_id,
                )
                price = 0

        logger.info(
            "change_access_type: updating DB room_id=%s access_type=%s price=%s",
            room_id,
            access_type,
            price,
        )

        await self.store.db_pool.runInteraction(
            "update_room_access_and_price",
            db.update_room_access_and_price,
            room_id,
            access_type,
            price,
        )

        logger.info(
            "change_access_type: success room_id=%s access_type=%s price=%s",
            room_id,
            access_type,
            price,
        )

        return {
            "room_id": room_id,
            "access_type": access_type,
            "price": price,
        }
    # ---------------- DELETE ROOM ---------------
    async def delete_room(self, *, requester, room_id: str):
        user_id = requester.user.to_string()

        logger.info(
            "delete_room: start room_id=%s requester=%s",
            room_id,
            user_id,
        )

        await self.assert_is_admin(user_id)

        row = await self.store.db_pool.runInteraction(
            "get_room_visibility",
            db.get_room_visibility,
            room_id,
        )

        if not row:
            logger.warning(
                "delete_room: room not found room_id=%s",
                room_id,
            )
            raise SynapseError(404, "Room not found")

        members = await self._admin_get_room_members(room_id)

        logger.info(
            "delete_room: kicking %d members from room_id=%s",
            len(members),
            room_id,
        )

        await self._admin_send_state(
            room_id,
            "m.room.tombstone",
            "",
            {
                "body": "This room has been deleted",
                "replacement_room": None,
            },
        )

        for member in members:
            if member == user_id:
                continue
            await self._admin_kick_user(
                room_id=room_id,
                target_user_id=member,
                reason="Room deleted",
            )

        await self._leave_room(room_id, requester)


        await self.store.db_pool.runInteraction(
            "delete_room_business",
            db.delete_room_business,
            room_id,
        )

        await self.store.db_pool.runInteraction(
            "remove_room_from_all_bundles",
            db.remove_room_from_all_bundles,
            room_id,
        )

        logger.info(
            "delete_room: success room_id=%s",
            room_id,
        )

        return {
            "room_id": room_id,
            "deleted": True,
        }

    # ---------------- INTERNAL ----------------
    def _community_gate(self):
        """Regra do community_gate, se o modulo estiver carregado."""
        return getattr(self.hs, "community_gate", None)

    async def _assert_gate_allows_join(self, user_id: str, room_id: str):
        """403 antes do admin join quando o community_gate nega o acesso.

        O spam checker ja barra o join em si; checar antes evita que o
        admin join falhe com um erro generico (500, ou 403 lido como
        "already_joined" em _admin_join).
        """
        gate = self._community_gate()
        if gate is not None and not await gate.may_join(user_id, room_id):
            logger.info("community_gate: room_service negou user=%s room=%s", user_id, room_id)
            raise SynapseError(403, "Not allowed to join this room")

    async def assert_is_admin(self, user_id: str):
        is_admin = await self.api.is_user_admin(user_id)

        if not is_admin:
            raise SynapseError(
                403,
                "Only Synapse admins can perform this action"
            )

    async def _ensure_admin(self, room_id: str):
        try:

            await self._admin_join(room_id, self.admin_user_id)
        except SynapseError as e:
            if e.code != 403:
                raise


    async def _admin_join(self, room_id: str, user_id: str):
        url = (
            f"{self.homeserver}/_synapse/admin/v1/join/"
            f"{quote(room_id)}"
        )

        payload = json.dumps({"user_id": user_id}).encode()

        response = await self.agent.request(
            b"POST",
            url.encode(),
            Headers({
                b"Authorization": [f"Bearer {self.admin_token}".encode()],
                b"Content-Type": [b"application/json"],
            }),
            bodyProducer=_BodyProducer(payload),
        )

        body = await readBody(response)

        body_str = body.decode(errors="ignore")

        logger.info(
            "admin_join: room=%s user=%s code=%s body=%s",
            room_id,
            user_id,
            response.code,
            body_str,
        )

        if response.code == 200:
            return "joined"

        if response.code == 403:
            return "already_joined"

        raise SynapseError(
            response.code,
            body_str,
        )

    async def _is_user_in_room(self, room_id: str, user_id: str) -> bool:
        users = await self.store.get_users_in_room(room_id)
        return user_id in users

    async def _admin_send_state(self, room_id: str, event_type: str, state_key: str, content: dict):
        url = (
            f"{self.homeserver}/_matrix/client/v3/rooms/"
            f"{quote(room_id)}/state/"
            f"{quote(event_type)}/"
            f"{quote(state_key)}"
        )

        payload = json.dumps(content).encode()

        response = await self.agent.request(
            b"PUT",
            url.encode(),
            Headers({
                b"Authorization": [f"Bearer {self.admin_token}".encode()],
                b"Content-Type": [b"application/json"],
            }),
            bodyProducer=_BodyProducer(payload),
        )

        body = await readBody(response)

        if response.code != 200:
            logger.error(
                "admin send_state failed %s code=%s body=%s",
                event_type,
                response.code,
                body.decode(errors="ignore"),
            )
            raise SynapseError(500, "admin send_state failed")

    async def _admin_get_room_members(self, room_id: str) -> list[str]:
        url = (
            f"{self.homeserver}/_matrix/client/v3/rooms/"
            f"{quote(room_id)}/members"
        )

        response = await self.agent.request(
            b"GET",
            url.encode(),
            Headers({
                b"Authorization": [f"Bearer {self.admin_token}".encode()],
            }),
        )

        body = await readBody(response)

        if response.code != 200:
            raise SynapseError(
                response.code,
                body.decode(errors="ignore"),
            )

        data = json.loads(body)
        return [
            ev["state_key"]
            for ev in data.get("chunk", [])
            if ev["type"] == "m.room.member"
            and ev["content"].get("membership") == "join"
        ]
        
    async def _admin_kick_user(
        self,
        *,
        room_id: str,
        target_user_id: str,
        reason: str,
    ):
        url = (
            f"{self.homeserver}/_matrix/client/v3/rooms/"
            f"{quote(room_id)}/kick"
        )

        payload = json.dumps({
            "user_id": target_user_id,
            "reason": reason,
        }).encode()

        response = await self.agent.request(
            b"POST",
            url.encode(),
            Headers({
                b"Authorization": [f"Bearer {self.admin_token}".encode()],
                b"Content-Type": [b"application/json"],
            }),
            bodyProducer=_BodyProducer(payload),
        )

        body = await readBody(response)

        if response.code != 200:
            logger.error(
                "kick failed room_id=%s user=%s code=%s body=%s",
                room_id,
                target_user_id,
                response.code,
                body.decode(errors="ignore"),
            )
            raise SynapseError(500, "Failed to kick user")

    async def _leave_room(self, room_id, requester):
        await self.room_member_handler.update_membership(
            requester=requester,
            target=requester.user,
            room_id=room_id,
            action="leave",
            ratelimit=False,
        )

    # ---------------- GET SPACE CHILDREN ----------------
    async def _get_space_children(self, space_id: str) -> list[str]:
        """
        Retorna os room_ids das salas que estão dentro do space.
 
        No Matrix, cada sala filha é um evento de estado do tipo
        'm.space.child' no space, onde o state_key é o room_id do filho.
        Um child só conta como ativo se o conteúdo tiver 'via' (quando o
        filho é removido, o Element manda um m.space.child com content
        vazio {} — sem 'via' — pra "desfazer" a relação).
        """
        try:
            state_events = await self.api.get_state_events_in_room(
                space_id,
                [("m.space.child", None)],   # None = todos os state_keys desse tipo
            )
        except Exception:
            logger.exception(
                "_get_space_children: failed to read children space_id=%s",
                space_id,
            )
            return []
 
        children = []
        for ev in state_events:
            # state_key é o room_id do filho
            child_id = ev.state_key
            # child ativo tem 'via'; content vazio = child removido
            via = ev.content.get("via")
            if child_id and via:
                children.append(child_id)
 
        logger.info(
            "_get_space_children: space_id=%s found %d children",
            space_id,
            len(children),
        )
        return children
 
 
    # ---------------- INVITE SPACE ----------------
    async def invite_space(self, user_id: str, space_id: str):
        """
        Dá ao usuário acesso a todas as salas dentro de um space pago.
        """
        logger.info(
            "invite_space: start user=%s space_id=%s",
            user_id,
            space_id,
        )

        await self._assert_gate_allows_join(user_id, space_id)

        room_ids = await self._get_space_children(space_id)
 
        if not room_ids:
            logger.warning(
                "invite_space: no child rooms found space_id=%s",
                space_id,
            )
            raise SynapseError(404, "Space not found or empty")
 
        logger.info(
            "invite_space: %d child rooms found space_id=%s",
            len(room_ids),
            space_id,
        )
 
        joined_rooms = []
 
        for room_id in room_ids:
            try:
                logger.debug(
                    "invite_space: joining user=%s room=%s",
                    user_id,
                    room_id,
                )
 
                result = await self._admin_join(room_id, user_id)
 
                if result in ("joined", "already_joined"):
                    joined_rooms.append(room_id)
 
            except Exception as e:
                logger.error(
                    "invite_space: failed join room=%s user=%s error=%s",
                    room_id,
                    user_id,
                    str(e),
                )
 
        logger.info(
            "invite_space: finished user=%s rooms_joined=%d",
            user_id,
            len(joined_rooms),
        )

        try:
            await self._admin_join(space_id, user_id)
        except Exception as e:
            logger.error(
                "invite_space: failed to join the space itself space=%s user=%s error=%s",
                space_id,
                user_id,
                str(e),
            )
 
        return joined_rooms

    async def list_space_children(self, space_id: str) -> list[dict]:
        """
        Retorna a lista completa das salas dentro de um space, cada uma com
        room_id, name e member_count. Usa o admin, então enxerga também as
        salas privadas (diferente do getRoomHierarchy do cliente).
        """
        logger.info("list_space_children: start space_id=%s", space_id)
 
        room_ids = await self._get_space_children(space_id)
 
        result = []
        for room_id in room_ids:
            # nome da sala (m.room.name)
            name = "Sem nome"
            try:
                state_events = await self.api.get_state_events_in_room(
                    room_id,
                    [("m.room.name", "")],
                )
                for ev in state_events:
                    name = ev.content.get("name", "Sem nome")
                    break
            except Exception:
                logger.warning(
                    "list_space_children: failed to read name room=%s",
                    room_id,
                )
 
            # contagem de membros
            member_count = 0
            try:
                users = await self.store.get_users_in_room(room_id)
                member_count = len(users)
            except Exception:
                logger.warning(
                    "list_space_children: failed to count members room=%s",
                    room_id,
                )
 
            result.append({
                "room_id": room_id,
                "name": name,
                "member_count": member_count,
            })
 
        logger.info(
            "list_space_children: space_id=%s returning %d rooms",
            space_id,
            len(result),
        )
        return result