"""Liga a regra do `gate.CommunityGate` ao spam checker do Synapse.

Config no homeserver.yaml:

    - module: modules.community_gate.module.CommunityGateModule
      config:
        internal_auth_provider: oidc-google   # opcional, padrao oidc-google
        community_spaces: ["!space:matrix.buzzlabs.com.br"]
        # opcional: funcionario entra sozinho nesse space no login
        internal_space: "!buzzlabs:matrix.buzzlabs.com.br"
        admin_user_id: "@scribe:matrix.buzzlabs.com.br"  # quem convida
"""

import logging

from synapse.api.errors import Codes
from synapse.config._base import ConfigError
from synapse.module_api import NOT_SPAM, ModuleApi

from .gate import CommunityGate

logger = logging.getLogger(__name__)


class SynapseBackend:
    """As quatro consultas que o gate faz, via ModuleApi."""

    def __init__(self, api: ModuleApi):
        self.api = api

    async def is_admin(self, user_id):
        return await self.api.is_user_admin(user_id)

    async def external_providers(self, user_id):
        # ModuleApi nao expoe external ids; o store expoe (mesmo acesso
        # interno que os outros modulos do fork ja fazem via api._hs).
        rows = await self.api._store.get_external_ids_by_user(user_id)
        return [provider for provider, _ in rows]

    async def _state(self, room_id, event_type):
        return await self.api.get_state_events_in_room(room_id, [(event_type, None)])

    async def space_child_events(self, room_id):
        events = await self._state(room_id, "m.space.child")
        return {ev.state_key: ev.content for ev in events}

    async def space_parent_events(self, room_id):
        events = await self._state(room_id, "m.space.parent")
        return {ev.state_key: ev.content for ev in events}

    async def memberships(self, room_id):
        events = await self._state(room_id, "m.room.member")
        return {ev.state_key: ev.content.get("membership") for ev in events}


def _verdict(allowed):
    return NOT_SPAM if allowed else Codes.FORBIDDEN


class CommunityGateModule:
    def __init__(self, config, api: ModuleApi):
        self.gate = CommunityGate(
            backend=SynapseBackend(api),
            internal_auth_provider=config["internal_auth_provider"],
            community_spaces=config["community_spaces"],
            internal_space=config["internal_space"],
        )
        self.api = api
        self.admin_user_id = config["admin_user_id"]
        # Publica a regra para o room_service (discover / invite / invite_space),
        # no mesmo padrao cross-module do room_streams_service.
        api._hs.community_gate = self.gate
        api.register_spam_checker_callbacks(
            check_event_for_spam=self.check_event_for_spam,
            user_may_join_room=self.user_may_join_room,
            user_may_invite=self.user_may_invite,
            user_may_create_room=self.user_may_create_room,
        )
        if config["internal_space"]:
            api.register_account_validity_callbacks(on_user_login=self.on_user_login)
        logger.info(
            "community_gate: ativo, %d space(s) de comunidade",
            len(config["community_spaces"]),
        )

    @staticmethod
    def parse_config(config):
        spaces = config.get("community_spaces")
        if not isinstance(spaces, list) or not all(isinstance(s, str) for s in spaces):
            raise ConfigError("community_gate: 'community_spaces' deve ser uma lista de room IDs")
        internal_space = config.get("internal_space")
        admin_user_id = config.get("admin_user_id")
        if internal_space and not admin_user_id:
            raise ConfigError("community_gate: 'internal_space' exige 'admin_user_id' (quem convida)")
        return {
            "internal_auth_provider": config.get("internal_auth_provider", "oidc-google"),
            "community_spaces": spaces,
            "internal_space": internal_space,
            "admin_user_id": admin_user_id,
        }

    async def on_user_login(self, user_id, auth_provider_type, auth_provider_id):
        """Coloca o funcionario no space interno. Nunca quebra o login."""
        try:
            if not await self.gate.should_join_internal_space(user_id, auth_provider_id):
                return
            space = self.gate.internal_space
            memberships = await self.gate.backend.memberships(space)
            if memberships.get(user_id) != "invite":
                await self.api.update_room_membership(self.admin_user_id, user_id, space, "invite")
            await self.api.update_room_membership(user_id, user_id, space, "join")
            logger.info("community_gate: %s entrou no space interno %s", user_id, space)
        except Exception:
            logger.exception("community_gate: falha ao colocar %s no space interno", user_id)

    async def check_event_for_spam(self, event):
        allowed = await self.gate.may_send_event(event.type, event.content, event.room_id)
        return _verdict(allowed)

    async def user_may_join_room(self, user_id, room_id, is_invited):
        return _verdict(await self.gate.may_join(user_id, room_id))

    async def user_may_invite(self, inviter, invitee, room_id):
        return _verdict(await self.gate.may_invite(inviter, invitee, room_id))

    async def user_may_create_room(self, user_id, room_config):
        return _verdict(await self.gate.may_create_room(user_id, room_config))
