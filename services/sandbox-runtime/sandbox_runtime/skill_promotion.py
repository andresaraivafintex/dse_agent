"""Pipeline de promoção de skill (WSC-E4-T2 + T3) — Fase 4.

Duas metades, ambas DETERMINÍSTICAS (P1 — nenhuma decisão de fluxo por LLM):

  1. **Captura de episódios + materialização de candidates (T2).** As três
     "sources at launch" (§10.17) gravam episódios em `skill_episode`:
     `clarification` (WS-B), `ci_repair` e `review_feedback` (WS-E). Quando um
     `pattern_key` acumula `SUM(occurrence_n) >= threshold` (config, não LLM),
     `materialize_candidates` cria uma skill em `skill_registry` com
     `status='candidate'`, `version` incrementada e proveniência completa. Um
     candidate NÃO é servido ao Planner (só {approved, active} são) e NÃO se
     auto-promove — precisa de eval + aprovação humana (P3).

  2. **Esteira de promoção governada (T3).** Máquina de estados explícita
     candidate → approved → canary → active (+ rollback active/canary →
     rolled_back). Invariantes NÃO-NEGOCIÁVEIS, por construção (não há code
     path que as viole — levantam exceção ANTES de qualquer escrita):
       - P1/P3: transição para {approved, active} exige `approver` humano
         resolvido e não-vazio (`ApproverRequired`); um `system:*` nunca aprova
         (nenhuma skill se auto-promove).
       - candidate → approved exige um eval PASSANTE (negative_regressions=0)
         registrado em `skill_eval` — a promoção fica bloqueada por construção
         se o replay regrediu num caso negativo (`EvalGateNotPassed`).
       - transição ilegal na máquina de estados levanta `IllegalTransition`.
     Rollback é mudança de PONTEIRO em uma transação (failure mode 13): a
     versão ativa vira `rolled_back` e a versão servida anterior volta a
     `active` — em segundos, sem reprocessar nada. O índice único parcial
     `uq_skill_registry_one_served` garante estruturalmente que nunca existam
     duas versões servidas da mesma skill.

Toda transição consequente grava em `dse_audit.emit` com a identidade do
aprovador (P8). Postgres real, falha limpa se indisponível (P6) — nunca
degrada silenciosamente para "sem skills".
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import psycopg2
from psycopg2.extras import Json

from dse_audit import emit as audit_emit

_DSN = os.environ.get(
    "DSE_SANDBOX_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)

# Fontes válidas de episódio (bate com o CHECK da migração 0019).
EPISODE_SOURCES = ("clarification", "ci_repair", "review_feedback")

# Limiar de materialização — CONFIG, não LLM (P1). Quantas occurrences do mesmo
# pattern_key até virar candidate.
DEFAULT_CANDIDATE_THRESHOLD = int(os.environ.get("DSE_SKILL_CANDIDATE_THRESHOLD", "3"))

# O que o Planner de PRODUÇÃO enxerga. candidate/canary/draft/rolled_back/retired
# NUNCA são servidos. canary = shadow (sem seleção de subconjunto canário nesta
# fase — documentado no README).
SERVED_STATUSES = ("approved", "active")

# Máquina de estados governada. Mapa from_status -> {to_status permitidos}.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "candidate": {"approved"},
    "approved": {"canary", "active", "retired"},
    "canary": {"active", "rolled_back"},
    "active": {"rolled_back"},
}

# Transições que exigem um aprovador HUMANO nomeado (P1/P3 — não-negociável).
APPROVER_REQUIRED_TO = {"approved", "active"}


class SkillPromotionUnavailable(Exception):
    """Postgres indisponível na esteira — falha limpa (P6)."""


class ApproverRequired(Exception):
    """Tentativa de promover para approved/active sem aprovador humano válido.
    Levantada ANTES de qualquer escrita — promoção sem humano é impossível por
    construção (P1/P3)."""


class EvalGateNotPassed(Exception):
    """candidate → approved sem um eval passante registrado (negative_regressions=0).
    Bloqueio por construção (P3 — o candidate não se aprova sozinho)."""


class IllegalTransition(Exception):
    """Transição fora da máquina de estados governada."""


class SkillNotFound(Exception):
    """(tenant, skill_key, version) inexistente."""


def _connect(dsn: str | None = None):
    try:
        return psycopg2.connect(dsn or _DSN)
    except Exception as exc:  # noqa: BLE001
        raise SkillPromotionUnavailable(f"skill_promotion: Postgres indisponível: {exc}") from exc


def _is_human_principal(approver: str | None) -> bool:
    """Aprovador precisa ser um principal humano resolvido — não vazio e não um
    ator de sistema (P3: nenhuma skill se auto-promove por um `system:*`)."""
    if not approver or not approver.strip():
        return False
    return not approver.strip().startswith("system:")


# ===========================================================================
# WSC-E4-T2 — captura de episódios + materialização de candidates
# ===========================================================================
def record_episode(
    tenant_id: str,
    source: str,
    pattern_key: str,
    *,
    work_item_id: str | None = None,
    occurrence_n: int = 1,
    provenance: dict[str, Any] | None = None,
    conn=None,
) -> int:
    """Grava um episódio de aprendizado (uma occurrence de um `pattern_key`).
    Determinístico e append-only — NÃO cria/ativa skill nenhuma aqui, é só o
    insumo governável. Retorna o id do episódio."""
    if source not in EPISODE_SOURCES:
        raise ValueError(f"source inválida: {source!r} (esperado {EPISODE_SOURCES})")
    if not tenant_id or not tenant_id.strip():
        raise ValueError("tenant_id é obrigatório")
    if not pattern_key or not pattern_key.strip():
        raise ValueError("pattern_key é obrigatório")

    owns = conn is None
    if owns:
        conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO skill_episode
                    (tenant_id, source, work_item_id, pattern_key, occurrence_n, provenance)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (tenant_id, source, work_item_id, pattern_key, occurrence_n, Json(provenance or {})),
            )
            episode_id = cur.fetchone()[0]
    finally:
        if owns:
            conn.close()
    return episode_id


def pattern_occurrence_counts(tenant_id: str, *, conn=None) -> dict[str, int]:
    """SUM(occurrence_n) por pattern_key do tenant. Base determinística do
    limiar de materialização."""
    owns = conn is None
    if owns:
        conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pattern_key, COALESCE(SUM(occurrence_n), 0)
                FROM skill_episode
                WHERE tenant_id = %s
                GROUP BY pattern_key
                """,
                (tenant_id,),
            )
            return {r[0]: int(r[1]) for r in cur.fetchall()}
    finally:
        if owns:
            conn.close()


def _candidate_skill_key(pattern_key: str) -> str:
    """skill_key determinístico de um pattern materializado (P1)."""
    return f"auto-{pattern_key}"


def _next_version(cur, tenant_id: str, skill_key: str) -> int:
    cur.execute(
        "SELECT COALESCE(MAX(version), 0) FROM skill_registry WHERE tenant_id = %s AND skill_key = %s",
        (tenant_id, skill_key),
    )
    return int(cur.fetchone()[0]) + 1


def _has_live_version(cur, tenant_id: str, skill_key: str) -> bool:
    """Já existe versão desta skill em estado 'vivo' (não rolled_back/retired)?
    Se sim, não re-materializa (idempotência da esteira)."""
    cur.execute(
        """
        SELECT 1 FROM skill_registry
        WHERE tenant_id = %s AND skill_key = %s
          AND status IN ('candidate', 'approved', 'canary', 'active')
        LIMIT 1
        """,
        (tenant_id, skill_key),
    )
    return cur.fetchone() is not None


@dataclass
class MaterializedCandidate:
    tenant_id: str
    skill_key: str
    version: int
    pattern_key: str
    occurrences: int


def _episode_provenance(cur, tenant_id: str, pattern_key: str) -> dict[str, Any]:
    cur.execute(
        """
        SELECT source, COUNT(*), COALESCE(SUM(occurrence_n), 0),
               COALESCE(array_agg(DISTINCT work_item_id) FILTER (WHERE work_item_id IS NOT NULL), '{}')
        FROM skill_episode
        WHERE tenant_id = %s AND pattern_key = %s
        GROUP BY source
        """,
        (tenant_id, pattern_key),
    )
    by_source: dict[str, Any] = {}
    work_items: set[str] = set()
    for source, n_rows, sum_occ, wi in cur.fetchall():
        by_source[source] = {"episodes": int(n_rows), "occurrences": int(sum_occ)}
        work_items.update(wi or [])
    return {
        "materialized_from": "skill_episode",
        "pattern_key": pattern_key,
        "by_source": by_source,
        "work_items": sorted(work_items),
    }


def materialize_candidates(
    tenant_id: str,
    *,
    threshold: int | None = None,
    conn=None,
    body_builder=None,
) -> list[MaterializedCandidate]:
    """WSC-E4-T2. Para cada pattern_key com occurrences >= threshold que ainda
    não tem versão viva, materializa uma skill CANDIDATE (status='candidate',
    version incrementada, proveniência dos episódios). 100% determinístico — o
    corpo é um template (`body_builder`, injetável), NUNCA gerado por LLM aqui.
    Idempotente: rodar de novo não duplica candidates.

    created_by = 'system:skill-promotion' de propósito: um candidate é uma
    PROPOSTA da máquina; só vira servível depois de eval + aprovação humana
    (P3). (A regra "created_by nunca system:*" da 0010 vale para os seeds já
    APROVADOS por humano, não para candidates propostos pela esteira.)"""
    thr = DEFAULT_CANDIDATE_THRESHOLD if threshold is None else threshold
    builder = body_builder or _default_candidate_body

    owns = conn is None
    if owns:
        conn = _connect()
    created: list[MaterializedCandidate] = []
    try:
        counts = pattern_occurrence_counts(tenant_id, conn=conn)
        with conn, conn.cursor() as cur:
            for pattern_key in sorted(counts):
                occ = counts[pattern_key]
                if occ < thr:
                    continue
                skill_key = _candidate_skill_key(pattern_key)
                if _has_live_version(cur, tenant_id, skill_key):
                    continue
                version = _next_version(cur, tenant_id, skill_key)
                prov = _episode_provenance(cur, tenant_id, pattern_key)
                prov["occurrences_at_materialization"] = occ
                title, body, category, applies_to = builder(pattern_key, prov)
                cur.execute(
                    """
                    INSERT INTO skill_registry
                        (tenant_id, skill_key, title, body, category, applies_to,
                         status, created_by, version, pattern_key, provenance)
                    VALUES (%s, %s, %s, %s, %s, %s, 'candidate', 'system:skill-promotion', %s, %s, %s)
                    """,
                    (
                        tenant_id, skill_key, title, body, category, Json(applies_to),
                        version, pattern_key, Json(prov),
                    ),
                )
                audit_emit(
                    actor="system:skill-promotion",
                    action="skill_candidate_materialized",
                    tenant_id=tenant_id,
                    details={
                        "skill_key": skill_key,
                        "version": version,
                        "pattern_key": pattern_key,
                        "occurrences": occ,
                        "threshold": thr,
                        "provenance": prov,
                    },
                    conn=conn,
                )
                created.append(
                    MaterializedCandidate(tenant_id, skill_key, version, pattern_key, occ)
                )
    finally:
        if owns:
            conn.close()
    return created


def _default_candidate_body(pattern_key: str, provenance: dict[str, Any]):
    """Template determinístico do corpo do candidate (P1). Retorna
    (title, body, category, applies_to)."""
    sources = ", ".join(sorted(provenance.get("by_source", {}).keys())) or "desconhecida"
    title = f"Padrão recorrente: {pattern_key}"
    body = (
        f"Skill CANDIDATE materializada automaticamente do pattern_key "
        f"'{pattern_key}' (fontes: {sources}). Requer eval + aprovação humana "
        f"antes de ser servida ao Planner. Proveniência completa em provenance."
    )
    return title, body, "auto", ["default"]


# ===========================================================================
# WSC-E4-T3 — eval do candidate (replay contra o eval set histórico)
# ===========================================================================
@dataclass
class EvalCase:
    """Um caso do eval set. `label` = 'positive' (a skill DEVERIA ajudar/disparar)
    ou 'negative' (a skill NÃO deve disparar). `pattern_key`/`text` descrevem o
    caso — o matcher determinístico decide se a skill dispara."""

    label: str  # 'positive' | 'negative'
    pattern_key: str
    text: str = ""


@dataclass
class EvalOutcome:
    passed: bool
    score: float
    positive_hits: int
    negative_regressions: int
    detail: str
    n_positive: int = 0
    n_negative: int = 0


def build_eval_set_from_episodes(
    tenant_id: str, candidate_pattern_key: str, *, conn=None
) -> list[EvalCase]:
    """Constrói um eval set a partir dos episódios do tenant:
      - POSITIVOS: episódios com o MESMO pattern_key do candidate (casos onde a
        skill ajudaria — ela deve disparar);
      - NEGATIVOS: episódios de OUTROS pattern_keys (casos onde a skill NÃO deve
        disparar — se disparar, é uma regressão).
    Determinístico e tenant-scoped."""
    owns = conn is None
    if owns:
        conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pattern_key, COALESCE(provenance->>'text', '') "
                "FROM skill_episode WHERE tenant_id = %s",
                (tenant_id,),
            )
            rows = cur.fetchall()
    finally:
        if owns:
            conn.close()
    cases: list[EvalCase] = []
    for pk, text in rows:
        label = "positive" if pk == candidate_pattern_key else "negative"
        cases.append(EvalCase(label=label, pattern_key=pk, text=text or ""))
    return cases


def _skill_fires(candidate_pattern_key: str, case: EvalCase) -> bool:
    """Matcher determinístico: a skill materializada do `candidate_pattern_key`
    dispara num caso cujo pattern_key é igual. (Trigger simples e auditável;
    substituível por um matcher mais rico atrás desta mesma função.)"""
    return case.pattern_key == candidate_pattern_key


def evaluate_candidate(
    tenant_id: str,
    skill_key: str,
    candidate_version: int,
    *,
    conn=None,
    eval_cases: list[EvalCase] | None = None,
    write_row: bool = True,
) -> EvalOutcome:
    """Replay do candidate contra o eval set (positivos + negativos),
    determinístico. `negative_regressions > 0` ⇒ `passed=False` (bloqueia
    promoção por construção). Grava a trilha em `skill_eval` (P8)."""
    owns = conn is None
    if owns:
        conn = _connect()
    try:
        # pattern_key do candidate (fonte de verdade: a linha materializada).
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pattern_key FROM skill_registry "
                "WHERE tenant_id = %s AND skill_key = %s AND version = %s",
                (tenant_id, skill_key, candidate_version),
            )
            row = cur.fetchone()
        if row is None:
            raise SkillNotFound(f"{tenant_id}/{skill_key} v{candidate_version} inexistente")
        candidate_pattern_key = row[0] or skill_key

        cases = (
            eval_cases
            if eval_cases is not None
            else build_eval_set_from_episodes(tenant_id, candidate_pattern_key, conn=conn)
        )
        n_pos = sum(1 for c in cases if c.label == "positive")
        n_neg = sum(1 for c in cases if c.label == "negative")

        positive_hits = 0
        negative_regressions = 0
        for c in cases:
            fired = _skill_fires(candidate_pattern_key, c)
            if c.label == "positive" and fired:
                positive_hits += 1
            elif c.label == "negative" and fired:
                negative_regressions += 1

        # score = recall nos positivos (0..1); só informativo. O GATE é
        # negative_regressions == 0 E pelo menos um positive_hit.
        score = (positive_hits / n_pos) if n_pos else 0.0
        passed = negative_regressions == 0 and positive_hits > 0
        detail = (
            f"positives={n_pos} negatives={n_neg} hits={positive_hits} "
            f"regressions={negative_regressions} score={score:.3f}"
        )

        if write_row:
            with conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO skill_eval
                        (tenant_id, skill_key, candidate_version, passed, score,
                         positive_hits, negative_regressions, detail)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (tenant_id, skill_key, candidate_version, passed, score,
                     positive_hits, negative_regressions, detail),
                )
            audit_emit(
                actor="system:skill-promotion",
                action="skill_candidate_evaluated",
                tenant_id=tenant_id,
                details={
                    "skill_key": skill_key,
                    "candidate_version": candidate_version,
                    "passed": passed,
                    "positive_hits": positive_hits,
                    "negative_regressions": negative_regressions,
                    "score": round(score, 6),
                },
                conn=conn,
            )
    finally:
        if owns:
            conn.close()

    return EvalOutcome(
        passed=passed,
        score=score,
        positive_hits=positive_hits,
        negative_regressions=negative_regressions,
        detail=detail,
        n_positive=n_pos,
        n_negative=n_neg,
    )


# ===========================================================================
# WSC-E4-T3 — promoção governada (state machine + rollback por ponteiro)
# ===========================================================================
@dataclass
class TransitionOutcome:
    skill_key: str
    version: int
    from_status: str
    to_status: str
    restored_version: int | None = None  # versão que voltou a active no rollback
    superseded_version: int | None = None  # versão servida anterior rebaixada na ativação


def _current_status(cur, tenant_id: str, skill_key: str, version: int) -> str:
    cur.execute(
        "SELECT status FROM skill_registry "
        "WHERE tenant_id = %s AND skill_key = %s AND version = %s FOR UPDATE",
        (tenant_id, skill_key, version),
    )
    row = cur.fetchone()
    if row is None:
        raise SkillNotFound(f"{tenant_id}/{skill_key} v{version} inexistente")
    return row[0]


def _has_passing_eval(cur, tenant_id: str, skill_key: str, version: int) -> bool:
    cur.execute(
        """
        SELECT 1 FROM skill_eval
        WHERE tenant_id = %s AND skill_key = %s AND candidate_version = %s
          AND passed = TRUE AND negative_regressions = 0
        LIMIT 1
        """,
        (tenant_id, skill_key, version),
    )
    return cur.fetchone() is not None


def _served_version(cur, tenant_id: str, skill_key: str, exclude_version: int) -> int | None:
    """A versão atualmente SERVIDA (approved/active) da skill, se houver, que
    não seja `exclude_version`. Há no máximo uma (índice único parcial)."""
    cur.execute(
        """
        SELECT version FROM skill_registry
        WHERE tenant_id = %s AND skill_key = %s AND version <> %s
          AND status IN ('approved', 'active')
        ORDER BY version DESC
        LIMIT 1
        FOR UPDATE
        """,
        (tenant_id, skill_key, exclude_version),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def _set_status(cur, tenant_id: str, skill_key: str, version: int, status: str) -> None:
    cur.execute(
        "UPDATE skill_registry SET status = %s "
        "WHERE tenant_id = %s AND skill_key = %s AND version = %s",
        (status, tenant_id, skill_key, version),
    )


def promote(
    tenant_id: str,
    skill_key: str,
    version: int,
    to_status: str,
    *,
    approver: str | None = None,
    reason: str = "",
    conn=None,
) -> TransitionOutcome:
    """Transição de estado GOVERNADA (WSC-E4-T3). Uma transação; ordem das
    escritas respeita o índice único parcial de "uma versão servida".

    Invariantes por construção (levantam ANTES de escrever):
      - to_status in {approved, active} sem approver humano ⇒ ApproverRequired.
      - candidate → approved sem eval passante ⇒ EvalGateNotPassed.
      - transição fora de ALLOWED_TRANSITIONS ⇒ IllegalTransition.

    Rollback (to_status='rolled_back'): a versão vira rolled_back e a versão
    servida anterior (registrada em provenance.supersedes) volta a active —
    mudança de PONTEIRO em segundos (failure mode 13)."""
    # --- Gate P1/P3: aprovador humano ANTES de qualquer escrita. ---
    if to_status in APPROVER_REQUIRED_TO and not _is_human_principal(approver):
        raise ApproverRequired(
            f"promoção para '{to_status}' exige aprovador humano nomeado "
            f"(recebido: {approver!r}) — P1/P3, impossível por construção"
        )

    owns = conn is None
    if owns:
        conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            from_status = _current_status(cur, tenant_id, skill_key, version)

            allowed = ALLOWED_TRANSITIONS.get(from_status, set())
            if to_status not in allowed:
                raise IllegalTransition(
                    f"{skill_key} v{version}: transição {from_status!r} -> {to_status!r} "
                    f"não permitida (permitidas: {sorted(allowed)})"
                )

            if from_status == "candidate" and to_status == "approved":
                if not _has_passing_eval(cur, tenant_id, skill_key, version):
                    raise EvalGateNotPassed(
                        f"{skill_key} v{version}: candidate -> approved bloqueado — "
                        f"nenhum eval passante (negative_regressions=0) registrado"
                    )

            outcome = TransitionOutcome(
                skill_key=skill_key, version=version,
                from_status=from_status, to_status=to_status,
            )

            if to_status in SERVED_STATUSES:
                # Entrar no conjunto SERVIDO ({approved, active}): rebaixa a
                # versão servida anterior (se houver, e for outra) ANTES de
                # servir esta — o índice único parcial `uq_..._one_served`
                # nunca vê duas versões servidas da mesma skill. A versão
                # suplantada vira 'retired' e fica registrada em
                # provenance.supersedes para o rollback devolver o ponteiro.
                prev = _served_version(cur, tenant_id, skill_key, exclude_version=version)
                if prev is not None:
                    _set_status(cur, tenant_id, skill_key, prev, "retired")
                    outcome.superseded_version = prev
                    cur.execute(
                        "UPDATE skill_registry "
                        "SET provenance = provenance || %s::jsonb "
                        "WHERE tenant_id = %s AND skill_key = %s AND version = %s",
                        (Json({"supersedes": prev}), tenant_id, skill_key, version),
                    )
                _set_status(cur, tenant_id, skill_key, version, to_status)

            elif to_status == "rolled_back":
                # Rollback por ponteiro: primeiro tira esta de servida, depois
                # restaura a versão que ela suplantou (se houver) a active.
                _set_status(cur, tenant_id, skill_key, version, "rolled_back")
                cur.execute(
                    "SELECT provenance->>'supersedes' "
                    "FROM skill_registry WHERE tenant_id = %s AND skill_key = %s AND version = %s",
                    (tenant_id, skill_key, version),
                )
                sup = cur.fetchone()[0]
                if sup is not None:
                    restored = int(sup)
                    # Só restaura se a versão suplantada ainda existir e estiver
                    # rebaixada (retired) — volta a ser o ponteiro servido.
                    cur.execute(
                        "SELECT status FROM skill_registry "
                        "WHERE tenant_id = %s AND skill_key = %s AND version = %s FOR UPDATE",
                        (tenant_id, skill_key, restored),
                    )
                    r = cur.fetchone()
                    if r is not None and r[0] in ("retired", "rolled_back", "approved"):
                        _set_status(cur, tenant_id, skill_key, restored, "active")
                        outcome.restored_version = restored
            else:
                # approved / canary: transição simples de status.
                _set_status(cur, tenant_id, skill_key, version, to_status)

            audit_emit(
                actor=approver if _is_human_principal(approver) else "system:skill-promotion",
                action="skill_promotion_transition",
                tenant_id=tenant_id,
                details={
                    "skill_key": skill_key,
                    "version": version,
                    "from_status": from_status,
                    "to_status": to_status,
                    "approver": approver,
                    "reason": reason,
                    "restored_version": outcome.restored_version,
                    "superseded_version": outcome.superseded_version,
                },
                conn=conn,
            )
    finally:
        if owns:
            conn.close()
    return outcome
