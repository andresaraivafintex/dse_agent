"""WSA-E1-T5 — Mapeamento plataforma -> tenant.

Resolve o `tenant_id` a partir de uma chave de plataforma (workspace Slack
`team_id`, installation da GitHub App `installation_id`, site Jira
`cloud_id`/base_url) consultando `tenant_platform_bindings`
(migrations/0008_wsa2.sql).

Fallback DOCUMENTADO (P6 decline-never-truncate, mas aqui é decline-para-o-
default-single-tenant, não truncate): binding ausente -> `DSE_TENANT_ID`
(env, default `tenant_dev`) COM uma audit row de aviso
(`action="tenant_binding_missing_fallback_default"`) — nunca adivinha um
tenant, sempre deixa evidência de que caiu no default. Quando o mapeamento
real existir (multi-tenant, §2.3 do adendo Fase 2), preencher
`tenant_platform_bindings` faz a resolução parar de cair no fallback sem
nenhuma mudança de código nos adapters.

Assinatura estável `(conn, platform, binding_key) -> ResolvedTenant` para o
WS-F trocar a fonte (identity map / ADR-22) sem quebrar os adapters.
"""
from __future__ import annotations

import os
from typing import NamedTuple

from dse_audit import emit as audit_emit


class ResolvedTenant(NamedTuple):
    tenant_id: str
    from_binding: bool  # True se veio de tenant_platform_bindings; False se fallback default


def default_tenant() -> str:
    """Tenant single-tenant de fallback (mesma env var que os adapters da
    Fase 1 já usavam em `config.get_tenant_id`)."""
    return os.environ.get("DSE_TENANT_ID", "tenant_dev")


def resolve_tenant(
    conn,
    *,
    platform: str,
    binding_key: str | None,
    audit_on_fallback: bool = True,
) -> ResolvedTenant:
    """Resolve o tenant de um evento de plataforma.

    `binding_key` ausente/None (ex.: um webhook que não carrega o
    identificador de workspace/installation/site) OU sem linha correspondente
    em `tenant_platform_bindings` -> cai no `DSE_TENANT_ID` default, emitindo
    uma audit row de aviso (a menos que `audit_on_fallback=False`, usado quando
    o caller já vai emitir seu próprio contexto).

    Nota de transação: usa `conn` para a leitura e para a audit row do
    fallback mas NÃO comita — segue a convenção de `dse_audit.emit(conn=...)`,
    o caller controla a fronteira de transação.
    """
    fallback = default_tenant()

    if binding_key:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id FROM tenant_platform_bindings WHERE platform = %s AND binding_key = %s",
                (platform, str(binding_key)),
            )
            row = cur.fetchone()
        if row is not None:
            return ResolvedTenant(row[0], True)

    if audit_on_fallback:
        audit_emit(
            actor=f"system:adapter-{platform}",
            action="tenant_binding_missing_fallback_default",
            tenant_id=fallback,
            details={"platform": platform, "binding_key": binding_key, "fallback_tenant_id": fallback},
            conn=conn,
        )
    return ResolvedTenant(fallback, False)
