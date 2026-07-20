"""WSB-E3-T2/T3 — a POLITICA do gate de aprovacao de plano, deliberadamente
FORA do modelo (P1: nenhuma decisao de fluxo por LLM). Duas responsabilidades,
ambas puras e deterministicas:

1. `classify_risk(plan, declared_risk)` — classe de risco EFETIVA. Comeca da
   classe declarada pelo Planner (WS-C) mas faz uma classificacao propria de
   defesa-em-profundidade: um plano que toca migracoes, workflows de CI, ou
   caminhos proibidos e SEMPRE `high`, ainda que o Planner tenha dito `low`
   (um modelo sub-classificando nao pode rebaixar o gate — P1). As categorias
   de alto risco do enunciado (auth/dinheiro/data model/migracao/cross-repo)
   sao mapeadas por sinais deterministicos nos paths declarados do plano.

2. `requires_plan_approval(risk_class, policy)` — dado a classe efetiva e a
   POLITICA (conjunto de classes que exigem aprovacao, vinda da config, nunca
   do modelo), decide se estaciona no gate ou auto-aprova.

O modulo NAO chama I/O — resolucao de aprovadores (que precisa de DB/GitHub)
vive numa Activity (`local_activities.resolve_plan_approver`), nunca no corpo
deterministico do workflow.
"""
from __future__ import annotations

from typing import Iterable

# Sinais deterministicos de alto risco por prefixo/substring de path. Espelha
# (e amplia) o `forbidden_paths` default do PlanArtifact. Nao e um modelo:
# e uma allowlist/denylist explicita, auditavel, versionada em codigo.
_HIGH_RISK_PATH_MARKERS: tuple[str, ...] = (
    "migrations/",          # data model / migracao
    ".github/workflows/",   # CI/CD supply chain
    "auth/",                # autenticacao/autorizacao
    "authz/",
    "billing/",             # dinheiro
    "payments/",
    "payment/",
    "iam/",
    "secrets",
)


def classify_risk(
    expected_files: Iterable[str] | None,
    declared_risk: str | None,
    *,
    cross_repo: bool = False,
) -> str:
    """Classe de risco efetiva. `high` domina: se qualquer sinal deterministico
    de alto risco estiver presente, o resultado e `high` independentemente do
    que o Planner declarou. Caso contrario, respeita a classe declarada
    (default `low`)."""
    files = list(expected_files or [])
    for path in files:
        p = (path or "").lower()
        for marker in _HIGH_RISK_PATH_MARKERS:
            if marker in p:
                return "high"
    if cross_repo:
        return "high"
    declared = (declared_risk or "low").strip().lower()
    if declared in ("high", "critical"):
        return "high"
    return declared or "low"


def requires_plan_approval(risk_class: str, require_classes: Iterable[str]) -> bool:
    """P1: decisao puramente deterministica de politica. `require_classes` vem
    da config do operador (WSB `require_approval_risk_classes`), NUNCA de um
    modelo."""
    normalized = {c.strip().lower() for c in require_classes}
    return (risk_class or "low").strip().lower() in normalized


# ---------------------------------------------------------------------------
# Cascata de resolucao de aprovador (WSB-E3-T2): CODEOWNERS -> designated
# approvers do access bundle (WS-F). Estes objetos rodam DENTRO de uma
# Activity (`resolve_plan_approver`) — precisam de I/O (ler CODEOWNERS via
# adapter GitHub / consultar `dse_access_bundles`), entao nunca sao chamados
# do corpo do workflow.
# ---------------------------------------------------------------------------

# CODEOWNERS reader trocavel. Producao: o adapter GitHub (WS-A) le o arquivo
# CODEOWNERS do repo via GitHub App. Aqui e um ponto de injecao: default
# retorna None (sem integracao GitHub local) — documentado como gap no README.
# Assinatura: (tenant_id, repo) -> texto CODEOWNERS ou None.
_codeowners_reader = None  # type: ignore[var-annotated]


def set_codeowners_reader(fn) -> None:
    """Injeta o reader de CODEOWNERS (producao: adapter GitHub; testes: fake)."""
    global _codeowners_reader
    _codeowners_reader = fn


def parse_codeowners_owners(text: str | None) -> list[str]:
    """Extrai a lista achatada de owners (logins/@teams) de um arquivo
    CODEOWNERS. Deterministico: sem glob-matching por path na Fase 2 (o gate
    de plano e por WorkItem, nao por arquivo) — retorna a UNIAO dos owners de
    todas as regras, deduplicada preservando ordem. Linhas de comentario/vazias
    ignoradas."""
    if not text:
        return []
    owners: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # formato: <pattern> <owner1> <owner2> ...
        parts = line.split()
        for token in parts[1:]:
            if token.startswith("@") or "@" in token:  # @user, @org/team, email
                if token not in seen:
                    seen.add(token)
                    owners.append(token)
    return owners
