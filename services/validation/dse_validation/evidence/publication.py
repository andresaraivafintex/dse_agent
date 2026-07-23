"""WSE-E5-T14 — publicação CONSOLIDADA e DEBOUNCED de evidência (ADR-26).

Um único tracking comment por PR (o mesmo `MutableCommentWriter` da fundação,
editado in-place — nunca comment-per-update) concentra TODOS os links de
evidência: vídeo @demo, trace Playwright, preview environment, visual diff.
O corpo é RENDERIZADO DO ESTADO DO BANCO (wse_artifacts / wse_previews /
wse_ci_status / wse_pr_tracking) — crash-consistente: qualquer re-render
converge para o mesmo conteúdo.

DEBOUNCE (ADR-26): re-gerar evidência é caro (Playwright + preview + diff).
`should_refresh_evidence()` é a decisão 100% DETERMINÍSTICA (P1) que o
workflow do WS-B consulta ANTES de re-rodar o pipeline de evidência:
  - pedido humano explícito           => refresh (sempre; auditado);
  - nenhuma publicação anterior       => refresh (primeira evidência);
  - mesmo commit já publicado         => NÃO refresh (no-op);
  - commit novo só com arquivos que não mudam comportamento (docs/markdown,
    default configurável)             => NÃO refresh;
  - commit novo com mudança de comportamento => refresh.
O lado do workflow (chamada da Activity) é do WS-B, em construção paralela —
o contrato de decisão exposto aqui é `wse_should_refresh_evidence`
(input/output documentados em activities.py).
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from fnmatch import fnmatch

from dse_validation import db
from dse_validation.evidence.garage import resolve_artifact_url

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

logger = logging.getLogger("dse_validation.evidence.publication")

# arquivos que NÃO mudam comportamento (não invalidam evidência) — ADR-26.
_DEFAULT_NON_BEHAVIOR_GLOBS = ["*.md", "docs/**", "**/*.md", "LICENSE", ".github/CODEOWNERS"]


def non_behavior_globs() -> list[str]:
    raw = os.environ.get("DSE_EVIDENCE_NON_BEHAVIOR_GLOBS", "")
    if raw.strip():
        return [g.strip() for g in raw.split(",") if g.strip()]
    return list(_DEFAULT_NON_BEHAVIOR_GLOBS)


def _matches_any(path: str, globs: list[str]) -> bool:
    for glob in globs:
        if fnmatch(path, glob) or (glob.startswith("**/") and fnmatch(path, glob[3:])):
            return True
    return False


@dataclass(frozen=True)
class RefreshDecision:
    refresh: bool
    reason: str


def should_refresh_evidence(
    *,
    work_item_id: str,
    commit_sha: str,
    files_changed: list[str] | None = None,
    human_requested: bool = False,
) -> RefreshDecision:
    """Decisão determinística de debounce (ADR-26). Ver docstring do módulo."""
    if human_requested:
        return RefreshDecision(True, "pedido humano explícito")
    previous = db.get_evidence_publication(work_item_id)
    if previous is None:
        return RefreshDecision(True, "primeira publicação de evidência")
    if previous["last_commit_sha"] == commit_sha:
        return RefreshDecision(False, "mesmo commit da última publicação (debounce ADR-26)")
    if files_changed is not None and files_changed and all(
        _matches_any(f, non_behavior_globs()) for f in files_changed
    ):
        return RefreshDecision(
            False, "commit novo só toca arquivos sem comportamento (docs) — debounce ADR-26"
        )
    return RefreshDecision(True, "commit novo com mudança de comportamento")


# ---------------------------------------------------------------------------
# Render do bloco de evidência + tracking comment consolidado
# ---------------------------------------------------------------------------
_EVIDENCE_HEADER = "### Fintex DSE — task evidence"

_KIND_LABEL = {
    "demo_video": "🎬 @demo video",
    "playwright_trace": "🧭 Playwright trace",
    "visual_diff": "🎨 Visual diff",
    "visual_baseline": "🖼️ Visual baseline",
    "test_report": "📄 Test report",
}


def render_evidence_section(work_item_id: str, *, accessor: str = "system:validation",
                            pr_number: int | None = None) -> str:
    """Monta a seção de evidência a partir do ESTADO DO BANCO. Cada link
    presigned é RESOLVIDO na hora (fresco) e o acesso é logado
    (`via='tracking_comment'`) — insumo da métrica evidence consumption."""
    lines: list[str] = [_EVIDENCE_HEADER, ""]

    artifacts = db.list_artifacts(work_item_id)
    for row in artifacts:
        if row["quarantined_at"] is not None:
            lines.append(f"- {_KIND_LABEL.get(row['kind'], row['kind'])}: **quarantined** (access revoked)")
            continue
        try:
            url = resolve_artifact_url(
                work_item_id=work_item_id, store_key=row["store_key"],
                accessor=accessor, pr_number=pr_number, via="tracking_comment",
            )
            expires = row["expires_at"].isoformat() if row["expires_at"] else "?"
            lines.append(
                f"- {_KIND_LABEL.get(row['kind'], row['kind'])}: [{row['store_key']}]({url}) "
                f"(expires {expires})"
            )
        except PermissionError as exc:
            lines.append(f"- {_KIND_LABEL.get(row['kind'], row['kind'])}: unavailable ({exc})")

    preview = db.get_preview(work_item_id)
    if preview is not None:
        if preview["status"] == "created":
            lines.append(f"- 🌐 Preview: {preview['url']} (namespace `{preview['namespace']}`, "
                         f"expires {preview['expires_at'].isoformat() if preview['expires_at'] else '?'})")
        elif preview["status"] == "skipped_backend_only":
            lines.append("- 🌐 Preview: skipped (backend-only PR, FR-20)")
        elif preview["status"] == "degraded":
            lines.append(f"- 🌐 Preview: degraded — {preview['detail'][:200]} (does not block the PR)")
        elif preview["status"] == "reaped":
            lines.append("- 🌐 Preview: expired (TTL)")

    ci = db.get_ci_status(work_item_id)
    if ci is not None:
        emoji = {"green": "🟢", "red": "🔴", "pending": "🟡"}.get(ci["status"], "⚪")
        lines.append(f"- {emoji} CI (L3): **{ci['status']}** (PR #{ci['pr_number']})")

    if len(lines) == 2:
        lines.append("_(no evidence published yet)_")
    return "\n".join(lines)


def publish_evidence_bundle(
    *,
    work_item_id: str,
    tenant_id: str,
    commit_sha: str,
    comment_writer,
    surface_ref: dict,
    pr_number: int | None = None,
    files_changed: list[str] | None = None,
    human_requested: bool = False,
    actor: str = "system:validation",
) -> dict:
    """Publicação consolidada + debounce. Retorna
    {published: bool, reason: str, fingerprint: str|None}."""
    decision = should_refresh_evidence(
        work_item_id=work_item_id, commit_sha=commit_sha,
        files_changed=files_changed, human_requested=human_requested,
    )
    if not decision.refresh:
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="evidence_refresh_debounced", tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"commit_sha": commit_sha, "reason": decision.reason},
            )
        return {"published": False, "reason": decision.reason, "fingerprint": None}

    body = render_evidence_section(work_item_id, accessor=actor, pr_number=pr_number)
    fingerprint = hashlib.sha256(body.encode()).hexdigest()[:16]
    comment_writer.upsert(work_item_id, surface_ref, body)
    db.upsert_evidence_publication(work_item_id, tenant_id, commit_sha, fingerprint)
    if audit_emit is not None:
        audit_emit(
            actor=actor, action="evidence_published", tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"commit_sha": commit_sha, "reason": decision.reason,
                     "fingerprint": fingerprint, "pr_number": pr_number},
        )
    return {"published": True, "reason": decision.reason, "fingerprint": fingerprint}
