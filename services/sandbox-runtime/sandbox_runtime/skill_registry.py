"""Skill registry bootstrap (WSC-E4-T1).

Registry tenant-scoped de skills curadas por humano, lido pela sessão Planner
(WSC-E3-T3) para hidratar contexto. Fase 2 tem SÓ o registry + a leitura — o
pipeline de promoção (curadoria automática de skills a partir de execuções) é
Fase 4, deliberadamente fora de escopo aqui.

Isolamento por tenant: `read_approved_skills(tenant_id)` só devolve linhas do
`tenant_id` pedido e `status='approved'` — o Planner nunca vê skills de outro
tenant nem rascunhos. Import de `psycopg2` é obrigatório aqui (a leitura de
skills é parte do contexto do Planner, não bookkeeping best-effort) — se o
Postgres cair, a leitura FALHA limpo (P6), nunca degrada para "sem skills"
silenciosamente. Use `allow_empty_on_unavailable=True` só em teste/dev.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import psycopg2

_DSN = os.environ.get(
    "DSE_SANDBOX_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


@dataclass(frozen=True)
class Skill:
    tenant_id: str
    skill_key: str
    title: str
    body: str
    category: str
    applies_to: list[str] = field(default_factory=list)

    def as_context_block(self) -> str:
        """Renderização determinística da skill para o bundle do Planner.
        Skills SÃO confiáveis (curadas por humano) — ao contrário do conteúdo
        de retrieval — então entram como guidance, não como dado untrusted."""
        applies = ", ".join(self.applies_to) if self.applies_to else "geral"
        return f"### skill:{self.skill_key} [{self.category}; aplica-se a: {applies}]\n{self.title}\n{self.body}"


class SkillRegistryUnavailable(Exception):
    """Postgres indisponível ao ler o registry — falha limpa (P6)."""


def _connect():
    return psycopg2.connect(_DSN)


def read_approved_skills(
    tenant_id: str,
    *,
    task_class: str | None = None,
    conn=None,
) -> list[Skill]:
    """Skills APROVADAS do tenant. Se `task_class` for dado, filtra para as que
    se aplicam a esse task_class (ou marcadas 'default'); senão devolve todas as
    aprovadas do tenant.

    ISOLAMENTO (coordenado com a suíte do WS-F): a query tem `tenant_id = %s`
    hardcoded — não existe caminho que devolva skill de outro tenant, e não há
    parâmetro para "todos os tenants". `status='approved'` também é hardcoded:
    rascunhos (`draft`) e aposentadas (`retired`) nunca são lidos pelo Planner.
    """
    owns = conn is None
    if owns:
        try:
            conn = _connect()
        except Exception as exc:  # noqa: BLE001
            raise SkillRegistryUnavailable(f"skill_registry: Postgres indisponível: {exc}") from exc
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tenant_id, skill_key, title, body, category, applies_to
                FROM skill_registry
                WHERE tenant_id = %s AND status = 'approved'
                ORDER BY category, skill_key
                """,
                (tenant_id,),
            )
            rows = cur.fetchall()
    finally:
        if owns:
            conn.close()

    skills = [
        Skill(
            tenant_id=r[0],
            skill_key=r[1],
            title=r[2],
            body=r[3],
            category=r[4],
            applies_to=list(r[5] or []),
        )
        for r in rows
    ]
    if task_class is not None:
        skills = [
            s
            for s in skills
            if not s.applies_to or task_class in s.applies_to or "default" in s.applies_to
        ]
    return skills
