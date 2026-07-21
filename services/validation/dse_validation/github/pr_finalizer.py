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

STRICT_COMMENT_TEMPLATE = """### Fintex DSE — branch pronto para revisão (modo estrito)

Os gates automáticos passaram (L1 + L2). Neste repositório o **PR é aberto por um
humano** — nenhum agente abre o PR (P1/modo estrito, WSE-E3-T8).

- **WorkItem**: `{work_item_id}`
- **Branch**: `{branch}` (já empurrado)
- **Abrir o PR (1 clique)**: {compare_url}

Ao abrir o PR a partir deste link, o Fintex DSE correlaciona e adota o PR
automaticamente (mesmo WorkItem) — o restante do fluxo (CI/status, merge humano)
segue igual.
"""

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


def compare_url_for(repo: str, base_branch: str, branch: str) -> str:
    """URL de compare do GitHub que abre o formulário de novo PR já preenchido
    (base <- head). `?expand=1` abre o form em vez da tela de diff (WSE-E3-T8)."""
    return f"https://github.com/{repo}/compare/{base_branch}...{branch}?expand=1"


def _adopt_and_ref(work_item_id: str, existing: dict, *, adopt: bool) -> PrRef:
    if adopt:
        db.adopt_tracked_pr(work_item_id, existing["number"], existing["html_url"])
    return PrRef(work_item_id=work_item_id, pr_number=existing["number"], url=existing["html_url"])


def adopt_pr_core(
    *,
    github_client: GitHubClient,
    work_item_id: str,
    tenant_id: str,
    repo: str,
    branch: str,
    pr_number: int | None = None,
    pr_url: str | None = None,
    actor: str = "system:validation",
) -> PrRef | None:
    """WSE-E3-T8 — chamado quando o workflow detecta que um humano abriu o PR a
    partir do compare link do modo estrito. Correlaciona pelo branch/WorkItem e
    ADOTA o PR (preenche pr_number na mesma linha de tracking — mesmo WorkItem).
    Idempotente: só adota se ainda não havia pr_number. Retorna o `PrRef` do PR
    adotado, ou `None` se nenhum PR aberto foi encontrado ainda."""
    if pr_number is None:
        found = github_client.get_open_pr_for_branch(repo, branch)
        if found is None:
            return None
        pr_number, pr_url = found["number"], found["html_url"]
    if pr_url is None:
        pr_url = f"https://github.com/{repo}/pull/{pr_number}"

    tracked = db.get_tracked_pr(work_item_id)
    already = tracked is not None and tracked.get("pr_number") is not None
    if tracked is None:
        db.save_tracked_pr(work_item_id, tenant_id, repo, branch, pr_number, pr_url)
    else:
        db.adopt_tracked_pr(work_item_id, pr_number, pr_url)

    if not already and audit_emit is not None:
        audit_emit(
            actor=actor,
            action="pr_adopted",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"repo": repo, "branch": branch, "pr_number": pr_number, "url": pr_url},
        )
    return PrRef(work_item_id=work_item_id, pr_number=pr_number, url=pr_url)


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
    strict_mode: bool = False,
    comment_writer=None,
    surface_ref: dict | None = None,
) -> PrRef:
    # 1) idempotência — nossa tabela é a fonte de verdade rápida.
    tracked = db.get_tracked_pr(work_item_id)
    if tracked is not None:
        if tracked.get("pr_number") is not None:
            # PR já aberto (por nós no modo normal, ou adotado no modo estrito).
            # REFINALIZE (fix cycle do review, observado ao vivo): este caminho
            # chega com commits NOVOS no branch — sem push, o fix do Coder
            # nunca aparecia no PR (no-op silencioso). O push é idempotente
            # (mesmo tip -> up-to-date), então empurrar sempre é seguro.
            if push:
                push_branch(executor, github_client, repo, branch)
            return PrRef(work_item_id=work_item_id, pr_number=tracked["pr_number"], url=tracked["pr_url"])
        # Modo estrito: compare link já postado, PR ainda não aberto. Um humano
        # pode ter aberto o PR nesse meio tempo — detecta e adota.
        existing = github_client.get_open_pr_for_branch(repo, branch)
        if existing is not None:
            return adopt_pr_core(
                github_client=github_client, work_item_id=work_item_id, tenant_id=tenant_id,
                repo=repo, branch=branch, pr_number=existing["number"], pr_url=existing["html_url"],
                actor=actor,
            )
        # Ninguém abriu ainda — reidempotente: devolve o MESMO compare link.
        cmp_url = tracked.get("compare_url") or compare_url_for(repo, base_branch, branch)
        return PrRef(work_item_id=work_item_id, pr_number=None, url=cmp_url, compare_url=cmp_url)

    # 2) defesa em profundidade: o GitHub pode já ter o PR se um run anterior
    #    morreu entre create_pr() e save_tracked_pr() (ou um humano já abriu).
    existing = github_client.get_open_pr_for_branch(repo, branch)
    if existing is not None:
        db.save_tracked_pr(work_item_id, tenant_id, repo, branch, existing["number"], existing["html_url"])
        return PrRef(work_item_id=work_item_id, pr_number=existing["number"], url=existing["html_url"])

    # 3) push do branch sob identidade da GitHub App (ambos os modos empurram).
    if push:
        push_branch(executor, github_client, repo, branch)

    # 3b) MODO ESTRITO (WSE-E3-T8): NÃO abre o PR — posta um compare link e para.
    if strict_mode:
        cmp_url = compare_url_for(repo, base_branch, branch)
        db.save_tracked_pr(
            work_item_id, tenant_id, repo, branch, pr_number=None, pr_url=cmp_url, compare_url=cmp_url
        )
        if comment_writer is not None and surface_ref is not None:
            comment_writer.upsert(
                work_item_id,
                surface_ref,
                STRICT_COMMENT_TEMPLATE.format(
                    work_item_id=work_item_id, branch=branch, compare_url=cmp_url
                ),
            )
        if audit_emit is not None:
            audit_emit(
                actor=actor,
                action="pr_compare_link_posted",
                tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"repo": repo, "branch": branch, "compare_url": cmp_url},
            )
        return PrRef(work_item_id=work_item_id, pr_number=None, url=cmp_url, compare_url=cmp_url)

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
