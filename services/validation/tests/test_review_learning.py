"""WSE-E6-T18 — episódios de skill-learning de review feedback aceito.

Postgres real (skill_episode da migração 0019 + audit). Prova:
  - feedback aceito repetido (mesmo padrão) => episódios com occurrence_n
    crescente, tenant-scoped;
  - proveniência completa (PR/reviewer/diff);
  - FRONTEIRA: NENHUMA skill é criada/ativada (skill_registry inalterado);
  - feedback NÃO aceito não vira episódio (P3);
  - isolamento por tenant do occurrence_n.
"""
from __future__ import annotations

import uuid

import pytest

from dse_validation import db
from dse_validation.review_learning import record_review_feedback_episode, review_pattern_key


@pytest.fixture
def tenant_id():
    return f"acme-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def work_item_id():
    return f"wi_{uuid.uuid4().hex[:8]}"


def test_repeated_accepted_feedback_increments_occurrence(tenant_id, work_item_id):
    body = "Use uma query parametrizada em vez de f-string no SQL."
    path = "app/dao.py"

    first = record_review_feedback_episode(
        tenant_id=tenant_id, work_item_id=work_item_id, pr_number=101,
        reviewer="usr_alice", comment_body=body, path=path,
        diff_hunk="- cur.execute(f\"...{x}\")\n+ cur.execute(\"...%s\", (x,))",
        accepted=True,
    )
    assert first is not None
    assert first["occurrence_n"] == 1

    # MESMO padrão, outro PR/work item — occurrence_n sobe (feedback repetido)
    wi2 = f"wi_{uuid.uuid4().hex[:8]}"
    second = record_review_feedback_episode(
        tenant_id=tenant_id, work_item_id=wi2, pr_number=102,
        reviewer="usr_bob", comment_body=body.upper(),  # normalização: mesmo padrão
        path=path, accepted=True,
    )
    assert second["occurrence_n"] == 2
    assert second["pattern_key"] == first["pattern_key"]

    episodes = db.list_skill_episodes(tenant_id, source="review_feedback",
                                      pattern_key=first["pattern_key"])
    assert len(episodes) == 2
    assert episodes[0]["provenance"]["reviewer"] == "usr_alice"
    assert episodes[0]["provenance"]["pr_number"] == 101
    assert "diff_hunk" in episodes[0]["provenance"]


def test_boundary_no_skill_created_or_activated(tenant_id, work_item_id):
    """A FRONTEIRA testada: gravar um episódio NÃO cria/ativa nenhuma skill."""
    before = db.count_skill_registry(tenant_id)

    ep = record_review_feedback_episode(
        tenant_id=tenant_id, work_item_id=work_item_id, pr_number=200,
        reviewer="usr_alice", comment_body="Adicione tratamento de None aqui.",
        path="app/svc.py", accepted=True,
    )
    assert ep is not None

    after = db.count_skill_registry(tenant_id)
    assert after == before, "gravar episódio NÃO pode criar/ativar skill (promoção é do WS-C)"

    # o episódio existe (o insumo governável)
    assert len(db.list_skill_episodes(tenant_id, source="review_feedback")) == 1
    # audit (P8)
    assert _audit_count(work_item_id, "review_feedback_episode_recorded") == 1


def test_not_accepted_feedback_records_nothing(tenant_id, work_item_id):
    result = record_review_feedback_episode(
        tenant_id=tenant_id, work_item_id=work_item_id, pr_number=300,
        reviewer="usr_alice", comment_body="talvez repensar isso", path="x.py",
        accepted=False,
    )
    assert result is None
    assert db.list_skill_episodes(tenant_id, source="review_feedback") == []
    assert _audit_count(work_item_id, "review_feedback_episode_recorded") == 0


def test_pattern_key_deterministic_and_path_scoped():
    a = review_pattern_key("Use  parametrized   query", "app/dao.py")
    b = review_pattern_key("use parametrized query", "app/dao.py")
    assert a == b  # normalização (lower + colapsa espaços)
    c = review_pattern_key("use parametrized query", "app/other.py")
    assert c != a  # path escopa o padrão


def test_tenant_scoped_occurrence(work_item_id):
    body = "Extraia essa constante mágica."
    t1 = f"acme-{uuid.uuid4().hex[:8]}"
    t2 = f"globex-{uuid.uuid4().hex[:8]}"
    e1 = record_review_feedback_episode(
        tenant_id=t1, work_item_id=work_item_id, pr_number=1, reviewer="usr_a",
        comment_body=body, path="c.py", accepted=True,
    )
    e2 = record_review_feedback_episode(
        tenant_id=t2, work_item_id=work_item_id, pr_number=1, reviewer="usr_a",
        comment_body=body, path="c.py", accepted=True,
    )
    # mesmo pattern_key (mesmo texto/path) mas occurrence_n reinicia por tenant
    assert e1["pattern_key"] == e2["pattern_key"]
    assert e1["occurrence_n"] == 1
    assert e2["occurrence_n"] == 1


def _audit_count(work_item_id: str, action: str) -> int:
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM audit_log WHERE work_item_id = %s AND action = %s",
                (work_item_id, action),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()
