"""WSE-E3-T6 — PR finalizer determinístico: após L1 verde, faz push do branch
sob identidade da GitHub App e abre EXATAMENTE 1 PR por WorkItem a partir de
um template fixo. P1 (deterministic-or-human): nenhuma parte desta função usa
um LLM para decidir nada — título/corpo vêm de um template fixo, a decisão de
"criar ou não" é puramente baseada no estado do banco/GitHub.

Idempotência (matar o processo no meio e re-rodar não deve criar 2º PR):
  1. confere `wse_pr_tracking` (nossa fonte de verdade rápida);
  2. se ausente, confere a API do GitHub por PR aberto com head=branch —
     cobre o caso do processo morrer DEPOIS de `create_pr()` e ANTES de
     `db.save_tracked_pr()` (a janela exata que o crash-test deste módulo
     exercita, ver tests/test_pr_finalizer_idempotent.py);
  3. só cria um PR novo se nenhuma das duas fontes tiver um já aberto.
"""
from __future__ import annotations

from dse_contracts import PrRef

from dse_validation import db
from dse_validation.github.client import GitHubClient
from dse_validation.sandbox_exec import SandboxExecutor

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

PR_TITLE_TEMPLATE = "[DSE {work_item_id}] {summary}"
PR_BODY_TEMPLATE = """### Fintex DSE — PR gerado automaticamente

- **WorkItem**: `{work_item_id}`
- **Risk class**: `{risk_class}`
- **Resumo**: {summary}
- **Evidência de teste (L1)**: {evidence_url}

{issue_link}

---
Este PR foi aberto por um agente autônomo (Fintex DSE). Nenhuma sessão de
agente aprova ou faz merge do próprio trabalho (P3) — revisão humana é
obrigatória antes do merge.
"""


def push_branch(executor: SandboxExecutor, github_client: GitHubClient, repo: str, branch: str, timeout: int = 60):
    remote_url = github_client.authenticated_remote_url(repo)
    result = executor.run(["git", "push", "--force-with-lease", remote_url, f"HEAD:refs/heads/{branch}"], timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"git push falhou (exit={result.returncode}): {result.stderr.strip()}")
    return result


def finalize_pr_core(
    *,
    executor: SandboxExecutor,
    github_client: GitHubClient,
    work_item_id: str,
    tenant_id: str,
    repo: str,
    branch: str,
    base_branch: str,
    summary: str,
    risk_class: str,
    evidence_url: str = "",
    issue_ref: dict | None = None,
    actor: str = "system:validation",
    push: bool = True,
) -> PrRef:
    # 1) idempotência — nossa tabela é a fonte de verdade rápida.
    tracked = db.get_tracked_pr(work_item_id)
    if tracked is not None:
        return PrRef(work_item_id=work_item_id, pr_number=tracked["pr_number"], url=tracked["pr_url"])

    # 2) defesa em profundidade: o GitHub pode já ter o PR se um run anterior
    #    morreu entre create_pr() e save_tracked_pr().
    existing = github_client.get_open_pr_for_branch(repo, branch)
    if existing is not None:
        db.save_tracked_pr(work_item_id, tenant_id, repo, branch, existing["number"], existing["html_url"])
        return PrRef(work_item_id=work_item_id, pr_number=existing["number"], url=existing["html_url"])

    # 3) push do branch sob identidade da GitHub App.
    if push:
        push_branch(executor, github_client, repo, branch)

    # 4) cria o PR a partir do template fixo — back-linka a issue de origem.
    issue_link = ""
    if issue_ref and issue_ref.get("issue_number"):
        issue_link = f"Closes #{issue_ref['issue_number']}"
    title = PR_TITLE_TEMPLATE.format(work_item_id=work_item_id, summary=summary)
    body = PR_BODY_TEMPLATE.format(
        work_item_id=work_item_id,
        risk_class=risk_class,
        summary=summary,
        evidence_url=evidence_url or "(sem link de evidência)",
        issue_link=issue_link or "(sem issue de origem vinculada)",
    )
    pr = github_client.create_pr(repo, head=branch, base=base_branch, title=title, body=body)

    db.save_tracked_pr(work_item_id, tenant_id, repo, branch, pr["number"], pr["html_url"])

    if audit_emit is not None:
        audit_emit(
            actor=actor,
            action="pr_finalized",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"repo": repo, "branch": branch, "pr_number": pr["number"], "url": pr["html_url"]},
        )

    return PrRef(work_item_id=work_item_id, pr_number=pr["number"], url=pr["html_url"])
