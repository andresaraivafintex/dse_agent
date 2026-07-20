"""WSF-E4-T3 — enforcement de isolamento multi-tenant (NFR-03), camada a camada.

Este módulo é a *primitiva reutilizável* de "toda leitura cruza a fronteira do
tenant que a pediu"; a *prova* (tentativas ativas de acesso cross-tenant que
DEVEM falhar e ser auditadas) vive na suíte `tests/test_tenant_isolation.py`.

Camadas cobertas (NFR-03):
  - filas        -> `fairness_key(tenant)` (namespacing determinístico da chave
                    de fairness worker-side lida pelo WS-B de `tenant_config`)
  - artifacts    -> `artifact_prefix(tenant)` (prefixo por tenant no store)
  - skills       -> `fetch_skill_scoped` (skill_registry do WS-C, tenant-scoped)
  - retrieval    -> `query_retrieval_scoped` (retrieval_documents do WS-C, E5)
  - audit        -> `query_audit_scoped` (partições por tenant do audit_log)
  - tokens       -> `assert_token_belongs_to_tenant` (virtual_keys do WS-D)

Toda tentativa de um tenant A ler um recurso do tenant B é NEGADA (deny) e
AUDITADA como `cross_tenant_access_denied` (P8). O guard central
`guard_same_tenant` é o "vazio/errado bloqueia" estrutural — nunca devolve dado
de outro tenant, nem silenciosamente (P6).
"""
from __future__ import annotations

import os
import re

import psycopg2
import psycopg2.extras
from dse_audit import emit

_DSN = os.environ.get(
    "DSE_PLATFORM_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)

_SAFE_TENANT = re.compile(r"^[A-Za-z0-9._-]+$")


def _get_connection():
    return psycopg2.connect(_DSN)


class CrossTenantViolation(Exception):
    """Um tenant tentou acessar um recurso de outro tenant. Fail-closed (P6)."""


def _valid_tenant(tenant_id: str) -> bool:
    return bool(tenant_id) and bool(_SAFE_TENANT.match(tenant_id))


def guard_same_tenant(
    *,
    requesting_tenant: str,
    resource_tenant: str | None,
    layer: str,
    resource_ref: str,
    actor: str = "system:tenant-isolation",
    conn=None,
) -> None:
    """Núcleo do enforcement: levanta `CrossTenantViolation` (e audita) se
    `resource_tenant` for None (recurso inexistente/oculto) ou diferente de
    `requesting_tenant`. Chamado por todos os accessors scoped abaixo."""
    if resource_tenant is not None and resource_tenant == requesting_tenant:
        return
    emit(
        actor=actor,
        action="cross_tenant_access_denied",
        tenant_id=requesting_tenant,
        details={
            "layer": layer,
            "resource_ref": resource_ref,
            "resource_tenant": resource_tenant,
            "requesting_tenant": requesting_tenant,
        },
        conn=conn,
    )
    raise CrossTenantViolation(
        f"tenant {requesting_tenant!r} tentou acessar {layer} {resource_ref!r} "
        f"do tenant {resource_tenant!r}"
    )


# ---------------------------------------------------------------------------
# Camada: filas (fairness keys) — namespacing determinístico
# ---------------------------------------------------------------------------
def fairness_key(tenant_id: str) -> str:
    """Chave de fairness worker-side (WS-B lê caps de `tenant_config` por esta
    chave). Namespacing forte por tenant: dois tenants NUNCA colidem. Rejeita
    tenant_id malformado (defesa contra injeção de separador)."""
    if not _valid_tenant(tenant_id):
        raise ValueError(f"tenant_id inválido para fairness_key: {tenant_id!r}")
    return f"tenant::{tenant_id}"


# ---------------------------------------------------------------------------
# Camada: artifacts — prefixo por tenant
# ---------------------------------------------------------------------------
def artifact_prefix(tenant_id: str) -> str:
    """Prefixo obrigatório de qualquer chave de artifact/objeto de um tenant.
    Rejeita tenant_id malformado (evita path traversal `../` no prefixo)."""
    if not _valid_tenant(tenant_id):
        raise ValueError(f"tenant_id inválido para artifact_prefix: {tenant_id!r}")
    return f"tenants/{tenant_id}/"


def artifact_key(tenant_id: str, relative_path: str) -> str:
    if ".." in relative_path or relative_path.startswith("/"):
        raise ValueError(f"path de artifact inseguro: {relative_path!r}")
    return artifact_prefix(tenant_id) + relative_path.lstrip("/")


# ---------------------------------------------------------------------------
# Camada: skills (skill_registry do WS-C) — tenant-scoped
# ---------------------------------------------------------------------------
def fetch_skill_scoped(requesting_tenant: str, skill_id: int, *, conn=None):
    """Busca uma skill por id, mas SÓ devolve se pertencer ao tenant que pede.
    Se a skill for de outro tenant, audita `cross_tenant_access_denied` e
    levanta `CrossTenantViolation` — nunca devolve a linha (o Planner de um
    tenant não pode carregar a skill de outro)."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM skill_registry WHERE id = %s", (skill_id,))
            row = cur.fetchone()
        resource_tenant = row["tenant_id"] if row else None
        guard_same_tenant(
            requesting_tenant=requesting_tenant,
            resource_tenant=resource_tenant,
            layer="skill",
            resource_ref=str(skill_id),
            conn=conn,
        )
        if owns:
            conn.commit()  # persistir o audit da violação (não alcançado se levantou)
        return dict(row)
    except Exception:
        if owns:
            conn.commit()  # audit da violação já foi emitido na mesma conn — preservar
        raise
    finally:
        if owns:
            conn.close()


# ---------------------------------------------------------------------------
# Camada: retrieval index (retrieval_documents do WS-C E5) — tenant-scoped
# ---------------------------------------------------------------------------
def query_retrieval_scoped(requesting_tenant: str, document_id: int, *, conn=None):
    """Igual a `fetch_skill_scoped`, para o índice de retrieval: um tenant não
    pode consultar um documento indexado de outro tenant."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM retrieval_documents WHERE id = %s", (document_id,))
            row = cur.fetchone()
        resource_tenant = row["tenant_id"] if row else None
        guard_same_tenant(
            requesting_tenant=requesting_tenant,
            resource_tenant=resource_tenant,
            layer="retrieval",
            resource_ref=str(document_id),
            conn=conn,
        )
        if owns:
            conn.commit()
        return dict(row)
    except Exception:
        if owns:
            conn.commit()
        raise
    finally:
        if owns:
            conn.close()


# ---------------------------------------------------------------------------
# Camada: audit (partições por tenant) — nenhuma query cruza tenant
# ---------------------------------------------------------------------------
def query_audit_scoped(requesting_tenant: str, target_tenant: str, *, conn=None):
    """Query de audit por tenant. Uma tentativa de um tenant consultar o audit
    de OUTRO tenant é negada + auditada. Só quando requesting == target a query
    roda (e devolve as linhas daquela partição)."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        guard_same_tenant(
            requesting_tenant=requesting_tenant,
            resource_tenant=target_tenant,
            layer="audit",
            resource_ref=f"audit_log[{target_tenant}]",
            conn=conn,
        )
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT action, actor, ts FROM audit_log WHERE tenant_id = %s ORDER BY ts DESC, id DESC LIMIT 100",
                (requesting_tenant,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        if owns:
            conn.commit()
        return rows
    except Exception:
        if owns:
            conn.commit()
        raise
    finally:
        if owns:
            conn.close()


# ---------------------------------------------------------------------------
# Camada: tokens (virtual_keys do WS-D)
# ---------------------------------------------------------------------------
def assert_token_belongs_to_tenant(requesting_tenant: str, key_alias: str, *, conn=None) -> None:
    """Confirma que a virtual key (por alias) pertence ao tenant que a apresenta.
    Um tenant apresentando o alias de key de outro tenant é negado + auditado."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT tenant_id FROM virtual_keys WHERE key_alias = %s", (key_alias,))
            row = cur.fetchone()
        resource_tenant = row[0] if row else None
        guard_same_tenant(
            requesting_tenant=requesting_tenant,
            resource_tenant=resource_tenant,
            layer="token",
            resource_ref=key_alias,
            conn=conn,
        )
        if owns:
            conn.commit()
    except Exception:
        if owns:
            conn.commit()
        raise
    finally:
        if owns:
            conn.close()
