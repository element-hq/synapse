"""Regra de isolamento entre internos e comunidades externas.

Sem dependencia do Synapse: as consultas ao homeserver chegam por um
`backend` (ver `module.SynapseBackend`), o que deixa a regra testavel
contra um mundo em memoria.
"""

import logging

logger = logging.getLogger(__name__)

# Profundidade maxima de spaces aninhados seguida a partir de um space de
# comunidade. Protege contra arvores grandes; ciclos ja sao tratados pelo
# conjunto de visitados.
MAX_SPACE_DEPTH = 5

ACTIVE_MEMBERSHIPS = ("join", "invite")

WIDGET_EVENT_TYPES = ("im.vector.modular.widgets", "m.widget")
JITSI_WIDGET_TYPES = ("m.jitsi", "jitsi")


class CommunityGate:
    def __init__(self, backend, internal_auth_provider, community_spaces, internal_space=None):
        self.backend = backend
        self.internal_auth_provider = internal_auth_provider
        self.community_spaces = list(community_spaces)
        self.internal_space = internal_space

    async def is_internal(self, user_id):
        """Interno = admin do servidor ou login pelo provider interno."""
        if await self.backend.is_admin(user_id):
            return True
        providers = await self.backend.external_providers(user_id)
        return self.internal_auth_provider in providers

    async def is_community_room(self, room_id):
        """Sala e um space de comunidade ou alcancavel por m.space.child dele."""
        visited = set()
        frontier = list(self.community_spaces)
        for _ in range(MAX_SPACE_DEPTH + 1):
            next_frontier = []
            for space_id in frontier:
                if space_id in visited:
                    continue
                visited.add(space_id)
                if space_id == room_id:
                    return True
                children = await self.backend.space_child_events(space_id)
                for child_id, content in children.items():
                    # m.space.child com content vazio (sem `via`) = filho removido
                    if not content.get("via"):
                        continue
                    if await self._child_confirms_parent(child_id, space_id):
                        next_frontier.append(child_id)
            if not next_frontier:
                return False
            frontier = next_frontier
        return False

    async def _child_confirms_parent(self, child_id, space_id):
        """O link space->filha so vale se a filha declara o space como pai.

        Quem tem PL no space da comunidade (o dono dela) consegue publicar
        m.space.child apontando para qualquer sala, inclusive interna. Ja o
        m.space.parent fica na propria sala filha, e so quem tem PL nela
        consegue grava-lo, entao exigir os dois lados impede que o dono da
        comunidade "adote" uma sala interna.
        """
        parents = await self.backend.space_parent_events(child_id)
        return bool((parents.get(space_id) or {}).get("via"))

    async def _is_community_only_room(self, room_id):
        """DM/sala cujos membros ativos sao todos da comunidade (e existem)."""
        memberships = await self.backend.memberships(room_id)
        active = [u for u, m in memberships.items() if m in ACTIVE_MEMBERSHIPS]
        if not active:
            return False
        for user_id in active:
            if await self.is_internal(user_id):
                return False
        return True

    async def may_join(self, user_id, room_id):
        if await self.is_internal(user_id):
            return True
        if await self.is_community_room(room_id):
            return True
        if await self._is_community_only_room(room_id):
            return True
        logger.info("community_gate: join negado user=%s room=%s", user_id, room_id)
        return False

    async def may_invite(self, inviter, invitee, room_id):
        # Convite do mesmo lado da fronteira e livre (interno->interno,
        # comunidade->comunidade = DM). Cruzando a fronteira, nos dois
        # sentidos, so para sala de comunidade.
        if await self.is_internal(inviter) == await self.is_internal(invitee):
            return True
        if await self.is_community_room(room_id):
            return True
        logger.info(
            "community_gate: convite negado inviter=%s invitee=%s room=%s",
            inviter,
            invitee,
            room_id,
        )
        return False

    async def should_join_internal_space(self, user_id, auth_provider_id):
        """Login pelo provider interno e ainda fora do space interno?

        As salas internas sao restricted -> membros do space interno, entao
        funcionario novo precisa entrar nele para enxerga-las. Decide pelo
        provider do proprio login (nao por consulta), e convite pendente
        ainda conta como "fora".
        """
        if not self.internal_space or auth_provider_id != self.internal_auth_provider:
            return False
        memberships = await self.backend.memberships(self.internal_space)
        return memberships.get(user_id) != "join"

    async def may_send_event(self, event_type, content, room_id):
        """Sem widget Jitsi em sala da comunidade (nem em DM so de comunidade).

        O meet transcreve toda conferencia e o call-scribe abre PR na
        kb-operacional; sem o widget nao ha call saindo da comunidade, seja
        qual for o cliente. Roda para todo evento do servidor: o que nao e
        widget Jitsi sai antes de qualquer consulta.
        """
        if event_type not in WIDGET_EVENT_TYPES:
            return True
        if (content or {}).get("type") not in JITSI_WIDGET_TYPES:
            return True  # outro widget, ou remocao (content vazio)
        if await self.is_community_room(room_id) or await self._is_community_only_room(room_id):
            logger.info("community_gate: widget Jitsi negado room=%s", room_id)
            return False
        return True

    async def may_create_room(self, user_id, room_config):
        if await self.is_internal(user_id):
            return True
        is_space = (room_config.get("creation_content") or {}).get("type") == "m.space"
        if room_config.get("is_direct") is True and not is_space:
            return True
        logger.info("community_gate: criacao de sala negada user=%s", user_id)
        return False
