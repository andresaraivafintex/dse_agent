"""WSF-E3-T2 — Access bundles por tenant/canal (§10.18).

Um *access bundle* é a unidade administrável que diz, para um (tenant, canal):
repos permitidos, modes habilitados, budgets, ações bloqueadas, approvers
designados (fallback da cascata CODEOWNERS do gate de plano do WS-B) e escopo
de learning (promoção de skill). Este módulo é tanto o CRUD administrável
quanto o *enforcement* consultado nos pontos de decisão dos outros workstreams.

Princípios:
- **P1 (deterministic-or-human)**: toda decisão aqui é comparação de conjuntos/
  strings em código — nenhum LLM decide se um repo/mode/ação é permitido.
- **P3 / cascata vazia bloqueia**: `resolve_plan_approvers` retorna a cascata
  (CODEOWNERS -> designated_approvers do bundle). Se o resultado for vazio, o
  caller DEVE bloquear (o gate de plano do WS-B nunca auto-aprova) — este
  módulo expõe `require_plan_approver` que levanta `NoApproverError` nesse caso,
  para que o "vazio bloqueia" seja estrutural e não uma checagem esquecível.
- **P8 (evidence over assertion)**: toda decisão de enforcement negativa
  (repo negado, ação bloqueada, mode não permitido, approver ausente) grava uma
  linha de audit via `dse_audit.emit`.

Resolução (tenant, canal): bundle específico do canal primeiro; se não existir
(ou desabilitado), cai para o bundle default do tenant (channel IS NULL). Se
nenhum existir, `get_effective_bundle` retorna None e o enforcement trata como
"nada permitido" (deny-by-default, nunca "sem limite").
"""
from __future__ import annotations

import dataclasses
import os
from typing import Any

import psycopg2
import psycopg2.extras
from dse_audit import emit

_DSN = os.environ.get(
    "DSE_PLATFORM_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)

VALID_MODES = {"scope", "ask", "implement_low_risk", "security_review"}


def _get_connection():
    return psycopg2.connect(_DSN)


class AccessDenied(Exception):
    """Enforcement negou uma operação (repo/mode/ação). Fail-closed em fronteira
    (P6: nunca segue meio-caminho)."""


class NoApproverError(Exception):
    """A cascata de approvers de plano resolveu vazia — o gate DEVE bloquear
    (P3: nunca auto-aprova). Levantada por `require_plan_approver`."""


@dataclasses.dataclass(frozen=True)
class AccessBundle:
    id: int
    tenant_id: str
    channel: str | None
    allowed_repos: list[str]
    modes: list[str]
    budgets: dict[str, Any]
    blocked_actions: list[str]
    designated_approvers: list[str]
    learning_scope: str
    enabled: bool


def _row_to_bundle(row) -> AccessBundle:
    return AccessBundle(
        id=row["id"],
        tenant_id=row["tenant_id"],
        channel=row["channel"],
        allowed_repos=list(row["allowed_repos"] or []),
        modes=list(row["modes"] or []),
        budgets=dict(row["budgets"] or {}),
        blocked_actions=list(row["blocked_actions"] or []),
        designated_approvers=list(row["designated_approvers"] or []),
        learning_scope=row["learning_scope"],
        enabled=row["enabled"],
    )


# ---------------------------------------------------------------------------
# CRUD administrável
# ---------------------------------------------------------------------------
def upsert_access_bundle(
    tenant_id: str,
    *,
    channel: str | None = None,
    allowed_repos: list[str] | None = None,
    modes: list[str] | None = None,
    budgets: dict[str, Any] | None = None,
    blocked_actions: list[str] | None = None,
    designated_approvers: list[str] | None = None,
    learning_scope: str | None = None,
    enabled: bool | None = None,
    actor: str = "system:platform-admin",
    conn=None,
) -> AccessBundle:
    """Cria/atualiza o bundle de (tenant, channel). Campos None não são tocados
    num update (idempotência parcial). Grava audit (P8)."""
    if modes is not None:
        invalid = set(modes) - VALID_MODES
        if invalid:
            raise ValueError(f"modes inválidos: {sorted(invalid)} (válidos: {sorted(VALID_MODES)})")
    if learning_scope is not None and learning_scope not in {"none", "tenant", "global"}:
        raise ValueError(f"learning_scope inválido: {learning_scope!r}")

    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            # channel NULL usa índice único parcial próprio; ON CONFLICT precisa
            # do índice correto conforme channel seja NULL ou não.
            if channel is None:
                conflict = "(tenant_id) WHERE channel IS NULL"
            else:
                conflict = "(tenant_id, channel) WHERE channel IS NOT NULL"
            cur.execute(
                f"""
                INSERT INTO dse_access_bundle
                    (tenant_id, channel, allowed_repos, modes, budgets,
                     blocked_actions, designated_approvers, learning_scope, enabled)
                VALUES (%s, %s,
                        COALESCE(%s::jsonb, '[]'::jsonb),
                        COALESCE(%s::jsonb, '[]'::jsonb),
                        COALESCE(%s::jsonb, '{{}}'::jsonb),
                        COALESCE(%s::jsonb, '[]'::jsonb),
                        COALESCE(%s::jsonb, '[]'::jsonb),
                        COALESCE(%s, 'none'),
                        COALESCE(%s, true))
                ON CONFLICT {conflict} DO UPDATE SET
                    allowed_repos = COALESCE(EXCLUDED.allowed_repos, dse_access_bundle.allowed_repos),
                    modes = COALESCE(EXCLUDED.modes, dse_access_bundle.modes),
                    budgets = COALESCE(EXCLUDED.budgets, dse_access_bundle.budgets),
                    blocked_actions = COALESCE(EXCLUDED.blocked_actions, dse_access_bundle.blocked_actions),
                    designated_approvers = COALESCE(EXCLUDED.designated_approvers, dse_access_bundle.designated_approvers),
                    learning_scope = COALESCE(EXCLUDED.learning_scope, dse_access_bundle.learning_scope),
                    enabled = COALESCE(EXCLUDED.enabled, dse_access_bundle.enabled)
                """,
                (
                    tenant_id,
                    channel,
                    _json_or_none(allowed_repos),
                    _json_or_none(modes),
                    _json_or_none(budgets),
                    _json_or_none(blocked_actions),
                    _json_or_none(designated_approvers),
                    learning_scope,
                    enabled,
                ),
            )
        emit(
            actor=actor,
            action="access_bundle_upserted",
            tenant_id=tenant_id,
            details={"channel": channel},
            conn=conn,
        )
        if owns:
            conn.commit()
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()

    result = get_bundle(tenant_id, channel=channel)
    assert result is not None
    return result


def _json_or_none(v):
    return None if v is None else psycopg2.extras.Json(v)


def get_bundle(tenant_id: str, *, channel: str | None = None, conn=None) -> AccessBundle | None:
    """Busca o bundle EXATO de (tenant, channel) — sem fallback. Use
    `get_effective_bundle` para a resolução com fallback."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if channel is None:
                cur.execute(
                    "SELECT * FROM dse_access_bundle WHERE tenant_id = %s AND channel IS NULL",
                    (tenant_id,),
                )
            else:
                cur.execute(
                    "SELECT * FROM dse_access_bundle WHERE tenant_id = %s AND channel = %s",
                    (tenant_id, channel),
                )
            row = cur.fetchone()
    finally:
        if owns:
            conn.close()
    return _row_to_bundle(row) if row else None


def get_effective_bundle(tenant_id: str, *, channel: str | None = None, conn=None) -> AccessBundle | None:
    """Bundle efetivo para (tenant, channel): específico do canal (se existir e
    enabled) senão o default do tenant (channel IS NULL, se enabled). None se
    nenhum — o enforcement trata None como deny-by-default."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        specific = None
        if channel is not None:
            specific = get_bundle(tenant_id, channel=channel, conn=conn)
            if specific is not None and specific.enabled:
                return specific
        default = get_bundle(tenant_id, channel=None, conn=conn)
        if default is not None and default.enabled:
            return default
        # canal específico existe mas está desabilitado, e não há default: sem bundle.
        return None
    finally:
        if owns:
            conn.close()


def list_bundles(tenant_id: str, conn=None) -> list[AccessBundle]:
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM dse_access_bundle WHERE tenant_id = %s ORDER BY channel NULLS FIRST",
                (tenant_id,),
            )
            rows = cur.fetchall()
    finally:
        if owns:
            conn.close()
    return [_row_to_bundle(r) for r in rows]


# ---------------------------------------------------------------------------
# Enforcement (consultado nos pontos de decisão)
# ---------------------------------------------------------------------------
def _audit_denial(tenant_id: str, work_item_id: str | None, action: str, details: dict, conn=None) -> None:
    emit(
        actor="system:platform-enforcement",
        action=action,
        tenant_id=tenant_id,
        work_item_id=work_item_id,
        details=details,
        conn=conn,
    )


def check_repo_allowed(
    tenant_id: str, repo: str, *, channel: str | None = None, work_item_id: str | None = None
) -> bool:
    """True sse `repo` está na allowlist do bundle efetivo. Ausência de bundle
    ou repo fora da lista = negado (deny-by-default) + audit."""
    bundle = get_effective_bundle(tenant_id, channel=channel)
    if bundle is not None and repo in bundle.allowed_repos:
        return True
    _audit_denial(
        tenant_id, work_item_id, "access_denied_repo",
        {"repo": repo, "channel": channel, "reason": "no_bundle" if bundle is None else "repo_not_in_allowlist"},
    )
    return False


def require_repo_allowed(tenant_id: str, repo: str, *, channel: str | None = None, work_item_id: str | None = None) -> None:
    if not check_repo_allowed(tenant_id, repo, channel=channel, work_item_id=work_item_id):
        raise AccessDenied(f"repo {repo!r} não permitido para tenant {tenant_id!r} (channel={channel!r})")


def check_mode_allowed(
    tenant_id: str, mode: str, *, channel: str | None = None, work_item_id: str | None = None
) -> bool:
    """True sse `mode` está habilitado no bundle efetivo. deny-by-default + audit."""
    if mode not in VALID_MODES:
        raise ValueError(f"mode desconhecido: {mode!r}")
    bundle = get_effective_bundle(tenant_id, channel=channel)
    if bundle is not None and mode in bundle.modes:
        return True
    _audit_denial(
        tenant_id, work_item_id, "access_denied_mode",
        {"mode": mode, "channel": channel, "reason": "no_bundle" if bundle is None else "mode_not_enabled"},
    )
    return False


def is_action_blocked(
    tenant_id: str, action: str, *, channel: str | None = None, work_item_id: str | None = None
) -> bool:
    """True sse `action` está em blocked_actions do bundle efetivo (ex.:
    "direct_merge_to_protected_branch"). Sem bundle => nada explicitamente
    bloqueado por lista, MAS o caller de uma ação sensível deve usar
    `require_action_allowed`, que também nega quando não há bundle."""
    bundle = get_effective_bundle(tenant_id, channel=channel)
    if bundle is None:
        return False
    blocked = action in bundle.blocked_actions
    if blocked:
        _audit_denial(
            tenant_id, work_item_id, "access_denied_action",
            {"action": action, "channel": channel, "reason": "action_in_blocklist"},
        )
    return blocked


def require_action_allowed(
    tenant_id: str, action: str, *, channel: str | None = None, work_item_id: str | None = None
) -> None:
    """Levanta AccessDenied se a ação está bloqueada OU se não há bundle
    (deny-by-default para ações sensíveis)."""
    bundle = get_effective_bundle(tenant_id, channel=channel)
    if bundle is None:
        _audit_denial(
            tenant_id, work_item_id, "access_denied_action",
            {"action": action, "channel": channel, "reason": "no_bundle"},
        )
        raise AccessDenied(f"ação {action!r} negada: sem access bundle para tenant {tenant_id!r}")
    if action in bundle.blocked_actions:
        _audit_denial(
            tenant_id, work_item_id, "access_denied_action",
            {"action": action, "channel": channel, "reason": "action_in_blocklist"},
        )
        raise AccessDenied(f"ação {action!r} bloqueada pelo bundle de {tenant_id!r} (channel={channel!r})")


# ---------------------------------------------------------------------------
# Cascata de approvers do gate de plano (WSB-E3-T2) — fallback do CODEOWNERS
# ---------------------------------------------------------------------------
def resolve_plan_approvers(
    tenant_id: str,
    *,
    channel: str | None = None,
    codeowners: list[str] | None = None,
    work_item_id: str | None = None,
    conn=None,
) -> list[str]:
    """Cascata do gate de plano: CODEOWNERS resolvidos (passados pelo WS-B a
    partir do repo) PRIMEIRO; se vazio/ausente, cai para os
    `designated_approvers` do bundle efetivo. Approvers offboardados
    (dse_console_identity.active = false) são REMOVIDOS da cascata (WSF-E3-T3).

    Retorna a lista final (pode ser vazia — o caller decide; `require_plan_approver`
    faz o "vazio bloqueia" estrutural)."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        candidates: list[str] = []
        for p in (codeowners or []):
            if p and p not in candidates:
                candidates.append(p)
        if not candidates:
            bundle = get_effective_bundle(tenant_id, channel=channel, conn=conn)
            if bundle is not None:
                for p in bundle.designated_approvers:
                    if p and p not in candidates:
                        candidates.append(p)

        # remove offboardados/expirados da resolução (WSF-E3-T3 offboarding)
        active = _filter_active_principals(candidates, conn=conn)
        removed = [p for p in candidates if p not in active]
        if removed:
            emit(
                actor="system:platform-enforcement",
                action="approvers_filtered_offboarded",
                tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"removed": removed, "channel": channel},
                conn=conn,
            )
            if owns:
                conn.commit()
        return active
    finally:
        if owns:
            conn.close()


def _filter_active_principals(principals: list[str], conn=None) -> list[str]:
    """Mantém só os principals que NÃO estão desativados no console identity.
    Um principal sem linha em dse_console_identity é considerado ativo (ele
    pode ser um CODEOWNER que nunca logou no console — não o removemos por
    isso; só removemos os EXPLICITAMENTE desativados/expirados)."""
    if not principals:
        return []
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT principal_id FROM dse_console_identity
                WHERE principal_id = ANY(%s)
                  AND (active = false OR (expires_at IS NOT NULL AND expires_at < now()))
                """,
                (principals,),
            )
            inactive = {r[0] for r in cur.fetchall()}
    finally:
        if owns:
            conn.close()
    return [p for p in principals if p not in inactive]


def require_plan_approver(
    tenant_id: str,
    *,
    channel: str | None = None,
    codeowners: list[str] | None = None,
    work_item_id: str | None = None,
) -> list[str]:
    """Igual a `resolve_plan_approvers`, mas levanta `NoApproverError` se a
    cascata resolver vazia — o "cascata vazia BLOQUEIA" do enunciado, feito
    estrutural (P1/P3: o gate nunca auto-aprova por falta de approver)."""
    approvers = resolve_plan_approvers(
        tenant_id, channel=channel, codeowners=codeowners, work_item_id=work_item_id
    )
    if not approvers:
        _audit_denial(
            tenant_id, work_item_id, "plan_gate_blocked_no_approver",
            {"channel": channel, "reason": "empty_cascade"},
        )
        raise NoApproverError(
            f"cascata de approvers vazia para tenant {tenant_id!r} (channel={channel!r}): gate bloqueado"
        )
    return approvers
