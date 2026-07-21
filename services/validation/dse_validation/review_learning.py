"""WSE-E6-T18 — emissão de episódios de skill-learning a partir de REVIEW
FEEDBACK aceito repetido.

Uma das três "sources at launch" de episódios (§10.17, tabela `skill_episode`
da migração 0019): clarificação recorrente (WS-B), CI-repair (WS-E, já entregue
na Fase 3 em `github/l3.py`), e **review feedback aceito** (WS-E, aqui).

O que faz: quando um feedback de review humano é ACEITO (o Coder aplicou a
mudança pedida e o revisor a aceitou), grava um `skill_episode` tenant-scoped
com `source='review_feedback'`, um `pattern_key` DETERMINÍSTICO (agrupa
ocorrências do mesmo padrão de feedback) e proveniência completa (PR, reviewer,
path, comentário, diff). `occurrence_n` conta quantas vezes o MESMO padrão
(tenant, pattern_key) já foi visto — é o sinal de "feedback repetido" que o
WS-C usa como insumo do pipeline de promoção (WSC-E4-T2/T3).

FRONTEIRA (testada, igual aos episódios de CI-repair): NENHUMA skill é criada
ou ativada aqui — só o episódio. A promoção candidate→eval→approved→canary→
active é 100% do WS-C, com aprovação humana (P3: nenhuma skill se auto-promove).

P1: `pattern_key` é uma normalização determinística de texto (nenhum LLM).
P3: só feedback ACEITO por um humano vira episódio (o produtor não gera seu
próprio sinal de aprendizado sozinho — o aceite é o gate humano).
P8: cada episódio emite uma linha de audit.
"""
from __future__ import annotations

import hashlib
import logging

from dse_validation import db

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

logger = logging.getLogger("dse_validation.review_learning")


def review_pattern_key(comment_body: str, path: str | None = None) -> str:
    """Assinatura DETERMINÍSTICA do padrão de feedback de review (tenant-scoped
    na tabela). Normaliza o texto (lower + colapsa espaços) e escopa
    frouxamente pelo path do arquivo comentado (o mesmo pedido no mesmo tipo de
    arquivo é o "mesmo padrão"). Hash curto, estável entre execuções.

    NÃO é semântica de LLM — é normalização de string pura (P1). Dois feedbacks
    com o mesmo texto normalizado no mesmo path colidem de propósito: é
    exatamente o "mesmo padrão repetido" que queremos contar."""
    normalized = " ".join((comment_body or "").lower().split())
    scope = (path or "").strip().lower()
    raw = f"{scope}|{normalized}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def record_review_feedback_episode(
    *,
    tenant_id: str,
    work_item_id: str,
    pr_number: int | None,
    reviewer: str,
    comment_body: str,
    path: str | None = None,
    diff_hunk: str | None = None,
    accepted: bool = True,
    actor: str = "system:validation",
) -> dict | None:
    """Grava o episódio de review-feedback (se `accepted`). Retorna
    `{pattern_key, id, occurrence_n}` ou `None` quando `accepted is False`
    (feedback não aceito não é sinal de aprendizado — P3).

    `reviewer` deve ser um principal humano resolvido (via dse_identity) — a
    proveniência precisa ser atribuível (P8) e o aceite é o gate humano (P3)."""
    if not accepted:
        logger.info(
            "review_learning %s: feedback não-aceito — nenhum episódio gravado", work_item_id
        )
        return None

    pattern_key = review_pattern_key(comment_body, path)
    provenance = {
        "pr_number": pr_number,
        "reviewer": reviewer,
        "path": path,
        "comment": comment_body,
        "diff_hunk": diff_hunk,
        "source": "review_feedback",
    }
    episode = db.record_review_feedback_episode(
        tenant_id=tenant_id,
        work_item_id=work_item_id,
        pattern_key=pattern_key,
        provenance=provenance,
    )

    if audit_emit is not None:
        audit_emit(
            actor=actor,
            action="review_feedback_episode_recorded",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={
                "pattern_key": pattern_key,
                "occurrence_n": episode["occurrence_n"],
                "pr_number": pr_number,
                "reviewer": reviewer,
                "path": path,
                "note": "episódio apenas — nenhuma skill criada/ativada (promoção é do WS-C, WSC-E4)",
            },
        )
    return {"pattern_key": pattern_key, **episode}
