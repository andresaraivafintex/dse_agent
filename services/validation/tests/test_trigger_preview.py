"""WSE-E4-T10 — previews por PR. Duas camadas de teste:

  1. Lógica determinística (paths-filter FR-20, caps ADR-26, degraded em
     falha) — rápida, contra Postgres real (nunca toca o cluster).
  2. Integração REAL contra o cluster k3d `dse-preview` (Argo CD v2.13.3 +
     ApplicationSet + git smart HTTP do docker-compose.wse.yml): namespace
     criado, URL respondendo, TTL destruindo via reaper GitOps. Marcada como
     o teste mais lento da suíte (~2-4min) — é o exit criterion da fase.
"""
from __future__ import annotations

import pytest

from dse_contracts.activities import TriggerPreviewInput

from dse_validation import db
from dse_validation.config import PreviewConfig
from dse_validation.preview.argocd import (
    get_preview_http_status,
    namespace_for,
    reap_expired_previews,
    trigger_preview_core,
    wait_namespace_gone,
)
from dse_validation.preview.paths_filter import (
    file_matches_glob,
    is_ui_touching,
    preview_decision,
)

DEFAULT_GLOBS = ["ui/**", "frontend/**", "**/*.css", "**/*.tsx", "**/*.jsx"]
DEPLOYABLE_GLOBS = [
    "**/Dockerfile", "Dockerfile", "**/*.py", "**/*.go", "**/*.rb",
    "**/*.java", "**/*.ts", "**/*.js", "k8s/**", "deploy/**", "charts/**",
    "**/requirements*.txt", "pyproject.toml", "go.mod", "package.json",
]


# ---------------------------------------------------------------------------
# 1a. paths-filter FR-20 (determinístico puro)
# ---------------------------------------------------------------------------
def test_backend_only_files_do_not_touch_ui():
    assert not is_ui_touching(["api/handler.py", "README.md", "migrations/0001.sql"], DEFAULT_GLOBS)


def test_ui_globs_match_expected_shapes():
    assert is_ui_touching(["frontend/app.tsx"], DEFAULT_GLOBS)
    assert is_ui_touching(["ui/components/button/index.js"], DEFAULT_GLOBS)  # ui/** aninhado
    assert is_ui_touching(["styles/app.css"], DEFAULT_GLOBS)  # **/*.css com diretório
    assert is_ui_touching(["app.css"], DEFAULT_GLOBS)  # **/*.css na RAIZ (ajuste documentado)
    assert not is_ui_touching([], DEFAULT_GLOBS)


def test_glob_semantics_documented():
    assert file_matches_glob("ui/a/b/c.js", "ui/**")
    assert file_matches_glob("web/x.tsx", "**/*.tsx")
    assert file_matches_glob("x.tsx", "**/*.tsx")
    assert not file_matches_glob("api/x.py", "ui/**")


def test_docs_only_pr_skips_and_counts_as_success(work_item_id, tenant_id):
    # Plano 08 §D: só docs (nem UI nem serviço deployável) → pula, conta como
    # sucesso, NUNCA bloqueia. (Antes um .py backend também pulava; agora ele
    # ganha preview — ver test_backend_service_change_now_previews.)
    ref = trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/app",
            pr_number=11, files_changed=["docs/x.md", "README.md", "CHANGELOG.md"],
        )
    )
    assert ref.status == "skipped_backend_only"  # conta como sucesso, NUNCA bloqueia
    row = db.get_preview(work_item_id)
    assert row["status"] == "skipped_backend_only"


# ---------------------------------------------------------------------------
# Plano 08 §D — decisão de previewabilidade (ui | deployable | none)
# ---------------------------------------------------------------------------
def test_preview_decision_ui_has_precedence():
    kind, hits = preview_decision(["frontend/app.tsx", "api/main.py"], DEFAULT_GLOBS, DEPLOYABLE_GLOBS)
    assert kind == "ui" and "frontend/app.tsx" in hits


def test_preview_decision_backend_service_is_deployable():
    kind, hits = preview_decision(["wallet/service.py", "Dockerfile"], DEFAULT_GLOBS, DEPLOYABLE_GLOBS)
    assert kind == "deployable" and hits


def test_preview_decision_docs_only_is_none():
    kind, hits = preview_decision(["docs/x.md", "README.md"], DEFAULT_GLOBS, DEPLOYABLE_GLOBS)
    assert kind == "none" and hits == []


def test_deploys_preview_gate_disabled_skips_without_touching_cluster(work_item_id, tenant_id):
    # repo não marcado deploys_preview → skipped_disabled no passo 0 (antes de
    # qualquer contato com o cluster). Prova: kube_context inválido, sem erro.
    cfg = PreviewConfig()
    cfg.kube_context = "context-invalido-prova-que-nao-toca-cluster"
    ref = trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/app",
            pr_number=15, files_changed=["frontend/app.tsx"], preview_enabled=False,
        ),
        cfg=cfg,
    )
    assert ref.status == "skipped_disabled"
    assert db.get_preview(work_item_id)["status"] == "skipped_disabled"


def test_backend_service_change_now_previews_reaches_provision(work_item_id, tenant_id, tmp_path):
    # D2: um PR de serviço backend (.py) NÃO é mais pulado como backend-only —
    # ele passa o paths-filter e chega ao provisionamento (que degrada aqui, sem
    # cluster; o ponto é que NÃO parou em skipped_backend_only).
    cfg = PreviewConfig()
    cfg.kube_context = "k3d-cluster-que-nao-existe"
    cfg.repo_dir = str(tmp_path / "repo")
    cfg.sync_timeout_s = 5
    ref = trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/svc",
            pr_number=16, files_changed=["wallet/service.py"],
        ),
        cfg=cfg,
    )
    assert ref.status == "degraded"  # chegou ao provisionamento (não pulou)
    assert ref.status != "skipped_backend_only"


def test_external_url_is_browser_reachable_when_configured():
    cfg = PreviewConfig()
    cfg.external_host_template = "https://{namespace}.preview.dse.local"
    assert cfg.preview_url_for("preview-wi-1") == "https://preview-wi-1.preview.dse.local"
    # sem template → DNS interno (link ainda aparece no PR — D1 — mas não clicável)
    cfg2 = PreviewConfig()
    cfg2.external_host_template = ""
    assert cfg2.preview_url_for("preview-wi-1").endswith(".svc.cluster.local")


# ---------------------------------------------------------------------------
# 1b. caps de concorrência por tenant (ADR-26, dia 1)
# ---------------------------------------------------------------------------
def test_concurrency_cap_counts_and_degrades(work_item_id, tenant_id):
    db.set_preview_cap(tenant_id, 2)
    # duas linhas "created" ativas do MESMO tenant (contagem real no Postgres)
    for i in range(2):
        db.upsert_preview(
            work_item_id=f"{work_item_id}-{i}", tenant_id=tenant_id, pr_number=i,
            repo="acme/app", status="created", namespace=f"preview-x-{i}",
        )
    assert db.count_active_previews(tenant_id) == 2
    ref = trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/app",
            pr_number=12, files_changed=["frontend/app.tsx"],
        )
    )
    assert ref.status == "degraded"
    assert "cap" in ref.detail
    # um preview reaped LIBERA a vaga
    db.mark_preview_reaped(f"{work_item_id}-0")
    assert db.count_active_previews(tenant_id) == 1


def test_cap_zero_blocks_immediately_without_touching_cluster(work_item_id, tenant_id):
    db.set_preview_cap(tenant_id, 0)
    cfg = PreviewConfig()
    cfg.kube_context = "context-inexistente-prova-que-nao-toca-o-cluster"
    ref = trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/app",
            pr_number=13, files_changed=["ui/x.css"],
        ),
        cfg=cfg,
    )
    assert ref.status == "degraded"  # e não levantou erro de kubectl => nunca chamou o cluster


# ---------------------------------------------------------------------------
# 1c. failure mode 9 — falha de preview => degraded (PR nunca bloqueia)
# ---------------------------------------------------------------------------
def test_cluster_failure_degrades_instead_of_blocking(work_item_id, tenant_id, tmp_path):
    cfg = PreviewConfig()
    cfg.kube_context = "k3d-cluster-que-nao-existe"
    cfg.repo_dir = str(tmp_path / "repo")  # repo isolado (não polui o real)
    cfg.sync_timeout_s = 5
    ref = trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/app",
            pr_number=14, files_changed=["frontend/app.tsx"],
        ),
        cfg=cfg,
    )
    assert ref.status == "degraded"
    assert ref.detail  # razão explícita (P6/P8)
    row = db.get_preview(work_item_id)
    assert row["status"] == "degraded"


# ---------------------------------------------------------------------------
# 2. Integração REAL contra o cluster k3d (exit criterion da fase)
# ---------------------------------------------------------------------------
def test_preview_e2e_real_cluster_create_serve_and_ttl_reap(work_item_id, tenant_id):
    """Contra o cluster k3d REAL: (a) ApplicationSet do Argo CD materializa o
    namespace efêmero com Deployment+Service; (b) a URL do preview responde
    HTTP 200; (c) TTL vencido => reaper GitOps remove do git e o namespace é
    DESTRUÍDO (prune + finalizer). ~2-4min."""
    ref = trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/app",
            pr_number=15, files_changed=["frontend/pages/index.tsx", "api/x.py"],
        ),
        ttl_seconds=1800,
    )
    assert ref.status == "created", ref.detail
    namespace = namespace_for(work_item_id)
    assert ref.namespace == namespace
    assert ref.url and namespace in ref.url

    # (b) URL respondendo de verdade (probe curl in-cluster contra o Service)
    assert get_preview_http_status(namespace) == 200

    # (c) TTL: força expiração no estado durável e roda o reaper determinístico
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE wse_previews SET expires_at = now() - interval '1 second' WHERE work_item_id = %s",
                (work_item_id,),
            )
        conn.commit()
    finally:
        conn.close()

    reaped = reap_expired_previews()
    assert work_item_id in reaped
    wait_namespace_gone(namespace, timeout_s=240)  # namespace destruído de verdade

    row = db.get_preview(work_item_id)
    assert row["status"] == "reaped" and row["reaped_at"] is not None

    # audit (P8): created + reaped
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action FROM audit_log WHERE work_item_id = %s "
                "AND action IN ('preview_created','preview_reaped') ORDER BY id",
                (work_item_id,),
            )
            actions = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    assert actions == ["preview_created", "preview_reaped"]
