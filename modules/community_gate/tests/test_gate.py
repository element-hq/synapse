import pytest

from modules.community_gate.gate import CommunityGate

# Testa a regra de isolamento entre internos e comunidade externa:
# 1. interno (login oidc-google) e admin entram em qualquer sala
# 2. comunidade so entra no space de comunidade e no que e alcancavel por
#    m.space.child a partir dele (inclusive subspaces), ou em DM so de comunidade
# 3. convite cruzando a fronteira (comunidade -> interno) so dentro da comunidade
# 4. comunidade so cria DM
#
# para testar: PYTHONPATH=. pytest -vv modules/community_gate/tests/

PROVIDER = "oidc-google"
ADMIN = "@scribe:hs"
STAFF = "@matheus:hs"
VICTOR = "@victor:hs"
FAN = "@fan:hs"
FAN2 = "@fan2:hs"

COMMUNITY_SPACE = "!victor-space:hs"
COMMUNITY_ROOM = "!victor-geral:hs"
COMMUNITY_SUBSPACE = "!victor-sub:hs"
COMMUNITY_DEEP_ROOM = "!victor-deep:hs"
INTERNAL_ROOM = "!geral:hs"
REMOVED_CHILD = "!removida:hs"


class FakeBackend:
    """Mundo em memoria com as quatro consultas que o gate faz ao Synapse."""

    def __init__(self):
        self.admins = {ADMIN}
        self.external_ids = {STAFF: [(PROVIDER, "google-sub-1")]}
        # room_id -> {state_key: content} dos eventos m.space.child
        self.space_children = {
            COMMUNITY_SPACE: {
                COMMUNITY_ROOM: {"via": ["hs"]},
                COMMUNITY_SUBSPACE: {"via": ["hs"]},
                REMOVED_CHILD: {},  # filho removido: content vazio
            },
            COMMUNITY_SUBSPACE: {COMMUNITY_DEEP_ROOM: {"via": ["hs"]}},
        }
        # room_id -> {user_id: membership}
        self.members = {
            INTERNAL_ROOM: {ADMIN: "join", STAFF: "join"},
            COMMUNITY_ROOM: {ADMIN: "join", VICTOR: "join"},
        }

    async def is_admin(self, user_id):
        return user_id in self.admins

    async def external_providers(self, user_id):
        return [p for p, _ in self.external_ids.get(user_id, [])]

    async def space_child_events(self, room_id):
        return self.space_children.get(room_id, {})

    async def memberships(self, room_id):
        return self.members.get(room_id, {})


def _gate(backend=None):
    return CommunityGate(
        backend=backend or FakeBackend(),
        internal_auth_provider=PROVIDER,
        community_spaces=[COMMUNITY_SPACE],
    )


# ---------------- quem e interno ----------------


@pytest.mark.asyncio
async def test_login_oidc_google_e_interno():
    assert await _gate().is_internal(STAFF) is True


@pytest.mark.asyncio
async def test_admin_sem_login_google_e_interno():
    assert await _gate().is_internal(ADMIN) is True


@pytest.mark.asyncio
async def test_usuario_sem_login_google_e_comunidade():
    assert await _gate().is_internal(FAN) is False


@pytest.mark.asyncio
async def test_outro_provider_nao_torna_interno():
    backend = FakeBackend()
    backend.external_ids[FAN] = [("oidc-github", "x")]
    assert await _gate(backend).is_internal(FAN) is False


# ---------------- join ----------------


@pytest.mark.asyncio
async def test_interno_entra_em_sala_interna():
    assert await _gate().may_join(STAFF, INTERNAL_ROOM) is True


@pytest.mark.asyncio
async def test_comunidade_nao_entra_em_sala_interna():
    assert await _gate().may_join(FAN, INTERNAL_ROOM) is False


@pytest.mark.asyncio
async def test_comunidade_entra_no_space_da_comunidade():
    assert await _gate().may_join(FAN, COMMUNITY_SPACE) is True


@pytest.mark.asyncio
async def test_comunidade_entra_em_sala_filha_do_space():
    assert await _gate().may_join(FAN, COMMUNITY_ROOM) is True


@pytest.mark.asyncio
async def test_comunidade_entra_em_sala_de_subspace():
    assert await _gate().may_join(FAN, COMMUNITY_DEEP_ROOM) is True


@pytest.mark.asyncio
async def test_filho_removido_do_space_nao_conta():
    assert await _gate().may_join(FAN, REMOVED_CHILD) is False


@pytest.mark.asyncio
async def test_ciclo_entre_spaces_nao_trava():
    backend = FakeBackend()
    backend.space_children[COMMUNITY_SUBSPACE][COMMUNITY_SPACE] = {"via": ["hs"]}
    assert await _gate(backend).may_join(FAN, INTERNAL_ROOM) is False


@pytest.mark.asyncio
async def test_comunidade_entra_em_dm_so_de_comunidade():
    backend = FakeBackend()
    backend.members["!dm:hs"] = {FAN2: "join", FAN: "invite"}
    assert await _gate(backend).may_join(FAN, "!dm:hs") is True


@pytest.mark.asyncio
async def test_comunidade_nao_entra_em_sala_com_interno_fora_da_comunidade():
    backend = FakeBackend()
    backend.members["!mista:hs"] = {FAN2: "join", STAFF: "join"}
    assert await _gate(backend).may_join(FAN, "!mista:hs") is False


@pytest.mark.asyncio
async def test_comunidade_nao_entra_em_sala_sem_membros():
    # sala vazia nao pode virar "DM so de comunidade" por vacuidade
    assert await _gate().may_join(FAN, "!vazia:hs") is False


@pytest.mark.asyncio
async def test_ex_membro_interno_nao_conta_como_membro():
    backend = FakeBackend()
    backend.members["!dm:hs"] = {FAN2: "join", STAFF: "leave", FAN: "invite"}
    assert await _gate(backend).may_join(FAN, "!dm:hs") is True


# ---------------- convite ----------------


@pytest.mark.asyncio
async def test_interno_convida_interno():
    backend = FakeBackend()
    backend.external_ids["@outro:hs"] = [(PROVIDER, "google-sub-2")]
    assert await _gate(backend).may_invite(STAFF, "@outro:hs", INTERNAL_ROOM) is True


@pytest.mark.asyncio
async def test_interno_nao_convida_comunidade_para_sala_interna():
    # evita convite pendurado que mostraria o nome da sala interna
    assert await _gate().may_invite(STAFF, FAN, INTERNAL_ROOM) is False


@pytest.mark.asyncio
async def test_interno_nao_convida_comunidade_para_dm():
    assert await _gate().may_invite(STAFF, FAN, "!dm:hs") is False


@pytest.mark.asyncio
async def test_interno_convida_comunidade_para_sala_da_comunidade():
    assert await _gate().may_invite(STAFF, FAN, COMMUNITY_ROOM) is True


@pytest.mark.asyncio
async def test_comunidade_convida_comunidade():
    assert await _gate().may_invite(FAN, FAN2, "!dm:hs") is True


@pytest.mark.asyncio
async def test_comunidade_nao_convida_interno_para_dm():
    assert await _gate().may_invite(FAN, STAFF, "!dm:hs") is False


@pytest.mark.asyncio
async def test_comunidade_convida_interno_para_sala_da_comunidade():
    assert await _gate().may_invite(VICTOR, STAFF, COMMUNITY_ROOM) is True


# ---------------- criacao de sala ----------------


@pytest.mark.asyncio
async def test_interno_cria_sala():
    assert await _gate().may_create_room(STAFF, {"name": "x"}) is True


@pytest.mark.asyncio
async def test_comunidade_cria_dm():
    assert await _gate().may_create_room(FAN, {"is_direct": True}) is True


@pytest.mark.asyncio
async def test_comunidade_nao_cria_sala_comum():
    assert await _gate().may_create_room(FAN, {"name": "x"}) is False


@pytest.mark.asyncio
async def test_comunidade_nao_cria_space_marcado_como_dm():
    config = {"is_direct": True, "creation_content": {"type": "m.space"}}
    assert await _gate().may_create_room(FAN, config) is False
