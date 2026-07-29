"""WSE-E4-T9b — full L3 (Phase 3): on top of Phase 1's minimal status-check
consumption (`ci_status.consume_ci_status_core`), it adds:

  1. REFLECTION — the aggregated CI status is reflected in the PR's single
     tracking comment (the foundation's own `MutableCommentWriter`, edited
     in-place) within the SAME consumption call => the reflection latency is the
     workflow's poll latency (WS-B calls the Activity on webhook/timer; the
     comment write happens inline, <1min by construction — proven by test).

  2. TARGETED RE-RUNS — on a fix commit (new sha after a `red` state), re-runs
     ONLY the check-runs that failed (`POST .../rerequest`, per job) instead of
     the whole suite (P5 cheapest-first). When the repo's CI does not support
     per-job re-run (403/422), it records and moves on — never blocks. Evidence
     in `wse_ci_reruns` + audit (P8).

  3. SKILL-LEARNING EPISODES — when a CI-repair pattern completes (`red` state on
     an older sha -> `green` on a newer sha), it emits a tenant-scoped EPISODE
     with provenance (`wse_ci_repair_episodes`). `occurrence_n` counts repetitions
     of the SAME pattern (tenant, failure_signature) — input to PHASE 4's skill
     promotion. NO skill is created/activated here.

Everything deterministic (P1) — no LLM decides anything in this module.
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
    """DETERMINISTIC signature of the failure pattern (tenant-scoped in the
    table): short hash of (check, conclusion, normalized 1st line of output)."""
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
    """Full L3 consumption: aggregate (Phase 1) + reflect in the tracking comment
    + targeted re-run on a fix commit + repair episode when red->green.
    The PREVIOUS state (status + ref + failures) comes from `wse_ci_status.detail`
    (persisted here additively — nothing from Phase 1 breaks)."""
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

    # persists (additively) the ref and the current failures for the next comparison.
    failed_now = [
        {"id": r.get("id"), "name": r.get("name"), "conclusion": r.get("conclusion"),
         "output_summary": (r.get("output") or {}).get("summary", "")}
        for r in _failed_runs(check_runs)
    ]
    # `save_ci_status` REPLACES detail wholesale (its upsert does
    # `detail = EXCLUDED.detail`, not a merge), so anything the core consumer
    # recorded and is not repeated here is lost. Carry the legacy-status
    # evidence forward rather than blanking it.
    previous = db.get_ci_status(work_item_id) or {}
    combined_summary = (previous.get("detail") or {}).get("combined_status")
    db.save_ci_status(
        work_item_id, pr_number, result.status,
        {
            "ref": ref,
            "failed_runs": failed_now,
            "check_runs": [
                {"name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion")}
                for r in check_runs
            ],
            "combined_status": combined_summary,
        },
    )

    # 1) REFLECTION in the single tracking comment (in-place, <1min by construction).
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

    # 2) TARGETED RE-RUNS — only the failed check-runs, only on a fix commit.
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
                "l3 %s: the repo CI does not support per-job re-run — continuing without re-run", work_item_id
            )

    # 3) CI-repair EPISODE — red on the previous sha -> green on the new sha.
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
                    "source": "wse_ci_status.detail",  # where the observation came from
                },
            )
            if audit_emit is not None:
                audit_emit(
                    actor=actor, action="ci_repair_episode_recorded", tenant_id=tenant_id,
                    work_item_id=work_item_id,
                    details={"check_name": run.get("name"), "failure_signature": sig,
                             "occurrence_n": episode["occurrence_n"],
                             "note": "episode only — no skill created/activated (Phase 4)"},
                )

    return result
