"""Cascata determinística de resolução de repositório (Relatório 07 C2 / Fase B).

Tarefas de Slack/Jira não nascem num repo (diferente do GitHub, onde a issue
JÁ está no repo). Esta cascata descobre o repo do MAIS específico/explícito
para o menos, e — crucialmente — devolve `(None, None)` quando não há
resolução confiável, para o workflow PERGUNTAR em vez de adivinhar. O repo
errado nunca acontece em silêncio (fail-safe; P1: só config + regex, nenhum LLM).

Ordem:
  1. override explícito (`repo=owner/name` no texto / campo dedicado)
  2. binding específico: channel (Slack) / component (Jira) / project (Jira)
  3. binding amplo: workspace (Slack team / Jira site)
  4. default single-repo do tenant (binding_value '*')
  5. nada -> (None, None) -> clarificação
"""
from __future__ import annotations

import re
from typing import Any

# override explícito no texto livre (mesmo formato do C4).
_RE_REPO = re.compile(r"\brepo(?:sitory)?\s*[:=]\s*([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)", re.I)
_RE_BRANCH = re.compile(r"\b(?:base[_-]?)?branch\s*[:=]\s*([A-Za-z0-9._/-]+)", re.I)

# precedência: índice menor = mais específico.
_TYPE_PRECEDENCE = ("channel", "component", "project", "workspace")


def parse_explicit_repo(text: str) -> tuple[str | None, str | None]:
    """(repo, base_branch) explícitos no texto, se houver. Rung 1 da cascata."""
    if not text:
        return None, None
    m = _RE_REPO.search(text)
    b = _RE_BRANCH.search(text)
    return (m.group(1) if m else None), (b.group(1) if b else None)


def resolve_repo(
    conn,
    *,
    tenant_id: str,
    platform: str,
    signals: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Executa a cascata. `signals` traz o que o adapter conseguiu extrair:
    {text, channel, component, project, workspace}. Retorna (repo, base_branch)
    ou (None, None) se nada resolve (=> o workflow pergunta).

    GitHub não usa isto (o repo vem do webhook); mantido genérico por simetria.
    """
    # Rung 1 — override explícito ganha de tudo.
    repo, branch = parse_explicit_repo(signals.get("text") or "")
    if repo:
        return repo, (branch or "main")

    # Rungs 2–3 — bindings, do mais específico ao mais amplo.
    candidates: list[tuple[str, str]] = []  # (binding_type, binding_value)
    for btype in _TYPE_PRECEDENCE:
        val = signals.get(btype)
        if val:
            candidates.append((btype, str(val)))
    if candidates:
        with conn.cursor() as cur:
            for btype, val in candidates:  # já em ordem de precedência
                cur.execute(
                    "SELECT repo, base_branch FROM repo_bindings "
                    "WHERE tenant_id = %s AND platform = %s AND binding_type = %s "
                    "AND binding_value = %s",
                    (tenant_id, platform, btype, val),
                )
                row = cur.fetchone()
                if row:
                    return row[0], row[1]

    # Rung 4 — default single-repo do tenant (binding_value '*'), qualquer
    # binding_type; se o tenant tem EXATAMENTE um repo distinto, usa ele.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT repo, base_branch FROM repo_bindings WHERE tenant_id = %s",
            (tenant_id,),
        )
        rows = cur.fetchall()
    distinct_repos = {r[0] for r in rows}
    if len(distinct_repos) == 1:
        only = rows[0]
        return only[0], only[1]

    # Rung 5 — ambíguo ou vazio: sem resolução confiável, pergunta (nunca
    # adivinha entre múltiplos repos).
    return None, None
