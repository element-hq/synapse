import pytest
from unittest.mock import MagicMock, AsyncMock

from synapse.api.errors import Codes
from synapse.module_api import NOT_SPAM

from modules.community_gate.module import CommunityGateModule, SynapseBackend

# Testa a ligacao do gate com o Synapse:
# 1. registra os tres callbacks do spam checker
# 2. traduz permitido -> NOT_SPAM e negado -> Codes.FORBIDDEN
# 3. valida a config
# 4. o backend converte eventos de estado no formato que o gate espera
#
# para testar (o pacote synapse precisa vir de uma instalacao, nao do repo):
#   PYTHONPATH=<dir so com o symlink modules/> pytest -vv modules/community_gate/tests/

CONFIG = {
    "internal_auth_provider": "oidc-google",
    "community_spaces": ["!victor-space:hs"],
}


def _event(state_key, content):
    ev = MagicMock()
    ev.state_key = state_key
    ev.content = content
    return ev


def _api(is_admin=False, providers=(), state=None):
    api = MagicMock()
    api.is_user_admin = AsyncMock(return_value=is_admin)
    api._store.get_external_ids_by_user = AsyncMock(
        return_value=[(p, "ext") for p in providers]
    )

    async def get_state(room_id, types):
        return (state or {}).get((room_id, types[0][0]), [])

    api.get_state_events_in_room = MagicMock(side_effect=get_state)
    return api


def _callbacks(api):
    CommunityGateModule(CommunityGateModule.parse_config(CONFIG), api)
    return api.register_spam_checker_callbacks.call_args.kwargs


def test_registra_callbacks_de_join_convite_e_criacao():
    callbacks = _callbacks(_api())
    assert set(callbacks) == {
        "user_may_join_room",
        "user_may_invite",
        "user_may_create_room",
    }


@pytest.mark.asyncio
async def test_join_permitido_vira_not_spam():
    callbacks = _callbacks(_api(providers=["oidc-google"]))
    assert await callbacks["user_may_join_room"]("@staff:hs", "!geral:hs", False) == NOT_SPAM


@pytest.mark.asyncio
async def test_join_negado_vira_forbidden():
    callbacks = _callbacks(_api())
    result = await callbacks["user_may_join_room"]("@fan:hs", "!geral:hs", True)
    assert result == Codes.FORBIDDEN


@pytest.mark.asyncio
async def test_convite_negado_vira_forbidden():
    api = _api()
    api.is_user_admin = AsyncMock(return_value=False)
    api._store.get_external_ids_by_user = AsyncMock(
        side_effect=lambda u: [("oidc-google", "x")] if u == "@staff:hs" else []
    )
    callbacks = _callbacks(api)
    result = await callbacks["user_may_invite"]("@fan:hs", "@staff:hs", "!dm:hs")
    assert result == Codes.FORBIDDEN


@pytest.mark.asyncio
async def test_criacao_de_dm_pela_comunidade_vira_not_spam():
    callbacks = _callbacks(_api())
    result = await callbacks["user_may_create_room"]("@fan:hs", {"is_direct": True})
    assert result == NOT_SPAM


def test_publica_o_gate_para_outros_modulos():
    api = _api()
    module = CommunityGateModule(CommunityGateModule.parse_config(CONFIG), api)
    assert api._hs.community_gate is module.gate


def test_config_exige_community_spaces():
    with pytest.raises(Exception):
        CommunityGateModule.parse_config({"internal_auth_provider": "oidc-google"})


def test_config_usa_oidc_google_por_padrao():
    config = CommunityGateModule.parse_config({"community_spaces": ["!a:hs"]})
    assert config["internal_auth_provider"] == "oidc-google"


@pytest.mark.asyncio
async def test_backend_le_filhos_do_space_por_state_key():
    api = _api(
        state={
            ("!s:hs", "m.space.child"): [
                _event("!a:hs", {"via": ["hs"]}),
                _event("!b:hs", {}),
            ]
        }
    )
    children = await SynapseBackend(api).space_child_events("!s:hs")
    assert children == {"!a:hs": {"via": ["hs"]}, "!b:hs": {}}


@pytest.mark.asyncio
async def test_backend_le_pais_da_sala_por_state_key():
    api = _api(
        state={("!r:hs", "m.space.parent"): [_event("!s:hs", {"via": ["hs"]})]}
    )
    parents = await SynapseBackend(api).space_parent_events("!r:hs")
    assert parents == {"!s:hs": {"via": ["hs"]}}


@pytest.mark.asyncio
async def test_backend_le_membership_por_usuario():
    api = _api(
        state={
            ("!r:hs", "m.room.member"): [
                _event("@a:hs", {"membership": "join"}),
                _event("@b:hs", {"membership": "leave"}),
            ]
        }
    )
    members = await SynapseBackend(api).memberships("!r:hs")
    assert members == {"@a:hs": "join", "@b:hs": "leave"}
