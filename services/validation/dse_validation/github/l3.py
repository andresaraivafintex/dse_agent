"""WSE-E4-T9b — L3 completo (Fase 3): em cima do consumo mínimo de status
checks da Fase 1 (`ci_status.consume_ci_status_core`), adiciona:

  1. REFLEXÃO — o status agregado do CI é refletido no tracking comment único
     do PR (o mesmo `MutableCommentWriter` da fundação, editado in-place) na
     MESMA chamada de consumo => a latência de reflexão é a latência do poll
     do workflow (WS-B chama a Activity ao receber webhook/timer; a escrita do
     comentário acontece inline, <1min por construção — provado por teste).

  2. TARGETED RE-RUNS — em fix commit (novo sha depois de um estado `red`),
     re-roda SÓ os check-runs que falharam (`POST .../rerequest`, por job) em
     vez da suíte inteira (P5 cheapest-first). Quando o CI do repo não suporta
     re-run por job (403/422), registra e segue — nunca bloqueia. Evidência em
     `wse_ci_reruns` + audit (P8).

  3. EPISÓDIOS DE SKILL-LEARNING — quando um padrão de CI-repair se completa
     (estado `red` num sha antigo -> `green` num sha novo), emite um EPISÓDIO
     tenant-scoped com proveniência (`wse_ci_repair_episodes`). `occurrence_n`
     conta as repetições do MESMO padrão (tenant, failure_signature) — insumo
     para a promoção de skills da FASE 4. NENHUMA skill é criada/ativada aqui.

Tudo determinístico (P1) — nenhum LLM decide nada neste módulo.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from dse_contracts import CiStatusResult

from dse_validation import db
from dse_validation.github.ci_status import _TERMINAL_FAILURE_CONCLUSIONS, consume_ci_status_core
from dse_validation.github.client import GitHubClient

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

logger = logging.getLogger("dse_validation.github.l3")

CI_COMMENT_TEMPLATE = """### Fintex DSE — CI (L3) for PR #{pr_number}

**Aggregate status**: {emoji} `{status}` — updated at {updated_at}

| check | status | conclusion |
|---|---|---|
{rows}

_Automated reflection by Fintex DSE (WSE-E4-T9b). No agent approves or merges
its own work (P3) — merging is always done by a human._
"""

_EMOJI = {"green": "🟢", "red": "🔴", "pending": "🟡"}


def failure_signature(check_name: str, conclusion: str, output_summary: str = "") -> str:
    """Assinatura DETERMINÍSTICA do padrão de falha (tenant-scoped na tabela):
    hash curto de (check, conclusão, 1ª linha do output normalizada)."""
    first_line = (output_summary or "").strip().splitlines()[0].strip().lower() if output_summary.strip() else ""
    raw = f"{check_name}|{conclusion}|{first_line}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def render_ci_comment(work_item_id: str, pr_number: int, status: str, check_runs: list[dict]) -> str:
    rows = "\n".join(
        f"| {r.get('name', '?')} | {r.get('status', '?')} | {r.get('conclusion') or '—'} |"
        for r in check_runs
    ) or "| _(no check runs yet)_ | | |"
    return CI_COMMENT_TEMPLATE.format(
        pr_number=pr_number,
        emoji=_EMOJI.get(status, "⚪"),
        status=status,
        updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        rows=rows,
    )


def _failed_runs(check_runs: list[dict]) -> list[dict]:
    return [
        r for r in check_runs
        if r.get("status") == "completed" and r.get("conclusion") in _TERMINAL_FAILURE_CONCLUSIONS
    ]


def consume_ci_status_l3(
    *,
    github_client: GitHubClient,
    work_item_id: str,
    tenant_id: str,
    repo: str,
    pr_number: int,
    ref: str,
    comment_writer=None,
    surface_ref: dict | None = None,
    actor: str = "system:validation",
) -> CiStatusResult:
    """Consumo L3 completo: agrega (Fase 1) + reflete no tracking comment +
    targeted re-run em fix commit + episódio de repair quando red->green.
    O estado ANTERIOR (status + ref + falhas) vem de `wse_ci_status.detail`
    (persistido aqui de forma aditiva — nada da Fase 1 quebra)."""
    previous = db.get_ci_status(work_item_id)
    prev_detail = (previous or {}).get("detail") or {}
    prev_status = (previous or {}).get("status")
    prev_ref = prev_detail.get("ref")
    prev_failed = prev_detail.get("failed_runs") or []

    result = consume_ci_status_core(
        github_client=github_client, work_item_id=work_item_id, tenant_id=tenant_id,
        repo=repo, pr_number=pr_number, ref=ref, actor=actor,
    )
    check_runs = github_client.list_check_runs(repo, ref)

    # persiste (aditivo) o ref e as falhas atuais para a próxima comparação.
    failed_now = [
        {"id": r.get("id"), "name": r.get("name"), "conclusion": r.get("conclusion"),
         "output_summary": (r.get("output") or {}).get("summary", "")}
        for r in _failed_runs(check_runs)
    ]
    db.save_ci_status(
        work_item_id, pr_number, result.status,
        {
            "ref": ref,
            "failed_runs": failed_now,
            "check_runs": [
                {"name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion")}
                for r in check_runs
            ],
        },
    )

    # 1) REFLEXÃO no tracking comment único (in-place, <1min por construção).
    if comment_writer is not None and surface_ref is not None:
        body = render_ci_comment(work_item_id, pr_number, result.status, check_runs)
        comment_writer.upsert(work_item_id, surface_ref, body)
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="ci_status_reflected", tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"pr_number": pr_number, "status": result.status, "ref": ref},
            )

    is_fix_commit = prev_ref is not None and prev_ref != ref and prev_status == "red"

    # 2) TARGETED RE-RUNS — só os check-runs falhos, só em fix commit.
    if is_fix_commit and failed_now:
        rerun_ids: list[int] = []
        rerun_names: list[str] = []
        for run in failed_now:
            if run["id"] is None:
                continue
            if github_client.rerequest_check_run(repo, int(run["id"])):
                rerun_ids.append(int(run["id"]))
                rerun_names.append(run["name"] or "?")
        if rerun_ids:
            db.record_ci_rerun(
                work_item_id=work_item_id, tenant_id=tenant_id, pr_number=pr_number,
                fix_commit_sha=ref, check_run_ids=rerun_ids, check_names=rerun_names,
            )
            if audit_emit is not None:
                audit_emit(
                    actor=actor, action="ci_targeted_rerun", tenant_id=tenant_id,
                    work_item_id=work_item_id,
                    details={"pr_number": pr_number, "fix_commit_sha": ref,
                             "check_run_ids": rerun_ids, "check_names": rerun_names},
                )
        else:
            logger.info(
                "l3 %s: CI do repo não suporta re-run por job — seguindo sem re-run", work_item_id
            )

    # 3) EPISÓDIO de CI-repair — red no sha anterior -> green no sha novo.
    if is_fix_commit and result.status == "green" and prev_failed:
        for run in prev_failed:
            sig = failure_signature(
                run.get("name") or "?", run.get("conclusion") or "?", run.get("output_summary") or ""
            )
            episode = db.record_ci_repair_episode(
                tenant_id=tenant_id, work_item_id=work_item_id,
                check_name=run.get("name") or "?", failure_signature=sig,
                fix_commit_sha=ref,
                provenance={
                    "repo": repo, "pr_number": pr_number,
                    "failed_ref": prev_ref, "fix_ref": ref,
                    "conclusion": run.get("conclusion"),
                    "source": "wse_ci_status.detail",  # de onde veio a observação
                },
            )
            if audit_emit is not None:
                audit_emit(
                    actor=actor, action="ci_repair_episode_recorded", tenant_id=tenant_id,
                    work_item_id=work_item_id,
                    details={"check_name": run.get("name"), "failure_signature": sig,
                             "occurrence_n": episode["occurrence_n"],
                             "note": "episódio apenas — nenhuma skill criada/ativada (Fase 4)"},
                )

    return result
