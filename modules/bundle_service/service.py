# service.py

import uuid

from synapse.api.errors import SynapseError

from . import db
import logging

logger = logging.getLogger(__name__)

class BundleService:
    def __init__(self, api, room_service=None):
        self.api = api
        self.store = api._hs.get_datastores().main
        self.room_service = room_service

    # ---------------- LIST BUNDLES ----------------
    async def list_bundles(self, user_id):
        logger.info("list_bundles: start for user %s", user_id)

        try:
            bundles = await self.store.db_pool.runInteraction(
                "list_bundles",
                db.list_bundles,
            )
        except Exception:
            logger.exception("list_bundles: DB error")
            raise

        logger.info("list_bundles: %d bundles found in DB", len(bundles))

        try:
            is_admin = await self._is_admin(user_id)
            logger.info("list_bundles: user %s admin=%s", user_id, is_admin)
        except Exception:
            logger.exception("list_bundles: failed to check admin status")
            is_admin = False

        result = []

        for bundle in bundles:

            if bundle["status"] == "draft":
                if bundle["created_by"] != user_id and not is_admin:
                    logger.info(
                        "list_bundles: skipping draft %s (not owner/admin)",
                        bundle["bundle_id"],
                    )
                    continue

            room_ids = bundle.get("rooms", [])

            try:
                room_business_rows = await self.store.db_pool.runInteraction(
                    "get_room_business_by_ids",
                    db.get_room_business_by_ids,
                    room_ids,
                )
            except Exception:
                logger.exception(
                    "list_bundles: failed to fetch room business for bundle %s",
                    bundle["bundle_id"],
                )
                room_business_rows = []

            room_keywords = {row[0]: row[1] for row in room_business_rows}

            rooms = []
            keywords = []

            for room_id in room_ids:
                try:
                    state_events = await self.api.get_state_events_in_room(
                        room_id,
                        [("m.room.name", "")]
                    )

                    name = "Sem nome"
                    for ev in state_events:
                        name = ev.content.get("name", "Sem nome")
                        break

                except Exception:
                    logger.warning(
                        "list_bundles: failed to fetch name for room %s",
                        room_id,
                    )
                    name = "Sem nome"

                # guarda room_id + name
                rooms.append({
                    "room_id": room_id,
                    "name": name
                })

                if room_id in room_keywords:
                    keywords.append(room_keywords[room_id])

            result.append({
                "bundle_id": bundle["bundle_id"],
                "bundle_name": bundle["bundle_name"],
                "price": bundle["price"],
                "rooms": rooms,
                "keywords": keywords,
                "status": bundle["status"],
            })

        logger.info("list_bundles: finished, %d bundles returned", len(result))

        return result


    async def _is_admin(self, user_id: str) -> bool:
        try:
            return await self.api.is_user_admin(user_id)
        except Exception:
            logger.exception("_is_admin: error checking admin for %s", user_id)
            return False
    

    # ---------------- CREATE BUNDLES ----------------
    async def create_bundle(self, bundle_name, price, created_by, rooms):
        logger.info(
            "create_bundle: start | name=%s | price=%s | created_by=%s | rooms_count=%d",
            bundle_name,
            price,
            created_by,
            len(rooms) if rooms else 0,
        )

        try:
            bundle_id = await self.store.db_pool.runInteraction(
                "create_bundle",
                db.create_bundle,
                bundle_name,
                price,
                created_by,
                rooms,
            )

            logger.info(
                "create_bundle: success | bundle_id=%s | created_by=%s",
                bundle_id,
                created_by,
            )

            return bundle_id

        except Exception as e:
            logger.exception(
                "create_bundle: failed | name=%s | created_by=%s",
                bundle_name,
                created_by,
            )
            raise
        
    # ---------------- PUBLISH BUNDLES ----------------
    async def publish_bundle(self, bundle_id: str, user_id: str):
        logger.info("Publishing bundle %s", bundle_id)

        is_admin = await self._is_admin(user_id)

        if not is_admin:
            logger.warning(
                "publish_bundle: permission denied | user=%s | bundle_id=%s",
                user_id,
                bundle_id,
            )
            raise SynapseError(403, "Only admins can publish bundles")


        exists = await self.store.db_pool.runInteraction(
            "bundle_exists",
            db.bundle_exists,
            bundle_id,
        )

        if not exists:
            raise SynapseError(404, "Bundle not found")

        await self.store.db_pool.runInteraction(
            "publish_bundle",
            db.publish_bundle,
            bundle_id,
        )

        return {"status": "published"}

    # ---------------- DELETE BUNDLE ----------------
    async def delete_bundle(self, bundle_id: str, user_id: str):
        logger.info("delete_bundle: start | bundle_id=%s | user=%s", bundle_id, user_id)

        is_admin = await self._is_admin(user_id)

        if not is_admin:
            logger.warning(
                "delete_bundle: permission denied | user=%s | bundle_id=%s",
                user_id,
                bundle_id,
            )
            raise SynapseError(403, "Only admins can delete bundles")

        exists = await self.store.db_pool.runInteraction(
            "bundle_exists",
            db.bundle_exists,
            bundle_id,
        )

        if not exists:
            logger.warning(
                "delete_bundle: bundle not found | bundle_id=%s",
                bundle_id,
            )
            raise SynapseError(404, "Bundle not found")

        await self.store.db_pool.runInteraction(
            "delete_bundle",
            db.delete_bundle,
            bundle_id,
        )

        logger.info(
            "delete_bundle: success | bundle_id=%s | deleted_by=%s",
            bundle_id,
            user_id,
        )

        return {"status": "deleted"}
    
    # ---------------- UPDATE BUNDLE ----------------
    async def update_bundle(self, user_id, bundle_id, bundle_name, price, rooms):
        logger.info("update_bundle: start bundle_id=%s user=%s", bundle_id, user_id)

        if not bundle_id:
            raise SynapseError(400, "bundle_id is required")

        if not bundle_name:
            raise SynapseError(400, "bundle_name is required")

        if price is None:
            raise SynapseError(400, "price is required")

        try:
            bundles = await self.store.db_pool.runInteraction(
                "get_bundle_by_id",
                db.get_bundle_by_id,
                bundle_id,
            )
        except Exception:
            logger.exception("update_bundle: failed fetching bundle")
            raise

        if not bundles:
            raise SynapseError(404, "Bundle not found")

        bundle = bundles[0]

        is_admin = await self._is_admin(user_id)

        if bundle["created_by"] != user_id and not is_admin:
            raise SynapseError(403, "Not allowed to update this bundle")

        rooms = list(set(rooms or []))

        try:
            await self.store.db_pool.runInteraction(
                "update_bundle",
                db.update_bundle,
                bundle_id,
                bundle_name,
                price,
                rooms,
            )
        except Exception:
            logger.exception("update_bundle: database failure")
            raise

        logger.info("update_bundle: success bundle_id=%s", bundle_id)

        return {"bundle_id": bundle_id}

    # ---------------- INVITE BUNDLE ----------------
    async def invite_bundle(self, user_id: str, bundle_id: str):
        logger.info(
            "invite_bundle: start user=%s bundle_id=%s",
            user_id,
            bundle_id,
        )
        
        if not self.room_service:
            raise SynapseError(500, "RoomService not configured")

        rows = await self.store.db_pool.runInteraction(
            "get_rooms_by_bundle",
            db.get_rooms_by_bundle,
            bundle_id,
        )

        if not rows:
            logger.warning(
                "invite_bundle: no rooms found bundle_id=%s",
                bundle_id,
            )
            raise SynapseError(404, "Bundle not found or empty")

        logger.info(
            "invite_bundle: %d rooms found bundle_id=%s",
            len(rows),
            bundle_id,
        )

        joined_rooms = []
        gate = self.room_service._community_gate()

        for row in rows:
            room_id = row[0] if isinstance(row, (list, tuple)) else row
            # Sala negada pelo community_gate nem chega ao admin join: ele
            # convida antes do join ser barrado e deixaria um convite pendurado.
            if gate is not None and not await gate.may_join(user_id, room_id):
                logger.info(
                    "community_gate: invite_bundle pulou room=%s user=%s",
                    room_id,
                    user_id,
                )
                continue
            try:
                logger.debug(
                    "invite_bundle: joining user=%s room=%s",
                    user_id,
                    room_id,
                )

                result = await self.room_service._admin_join(room_id, user_id)

                if result in ("joined", "already_joined"):
                    joined_rooms.append(room_id)

            except Exception as e:
                logger.error(
                    "invite_bundle: failed join room=%s user=%s error=%s",
                    room_id,
                    user_id,
                    str(e),
                )

        logger.info(
            "invite_bundle: finished user=%s rooms_joined=%d",
            user_id,
            len(joined_rooms),
        )

        return joined_rooms

    