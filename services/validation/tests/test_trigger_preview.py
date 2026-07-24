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
# Plano 08 §D — D3 (Ingress) + D4 (imagem do PR) nos manifests
# ---------------------------------------------------------------------------
def _manifests(cfg, **kw):
    from datetime import datetime, timedelta, timezone
    from dse_validation.preview.argocd import build_manifests
    exp = datetime.now(timezone.utc) + timedelta(seconds=600)
    return build_manifests("preview-wi-x", "wi-x", "tenant_dev", exp, 600, cfg, **kw)


def test_ingress_generated_with_hostname_from_template():
    cfg = PreviewConfig()
    cfg.external_host_template = "http://{namespace}.preview.localhost:8081"
    m = _manifests(cfg)
    assert "ingress.yaml" in m
    assert "host: preview-wi-x.preview.localhost" in m["ingress.yaml"]  # sem scheme/porta
    assert "ingressClassName: traefik" in m["ingress.yaml"]


def test_no_ingress_without_external_host():
    cfg = PreviewConfig()
    cfg.external_host_template = ""
    assert "ingress.yaml" not in _manifests(cfg)


def test_label_values_respect_k8s_63_char_limit():
    """Disparo real (issue #2 → PR #10): work_item_id `wi_`+64 hex = 67 chars
    virava valor de label e o k8s REJEITAVA o namespace (label value ≤ 63) —
    o preview degradava sem URL. O id completo vai na annotation; a label é
    truncada."""
    import re
    from datetime import datetime, timedelta, timezone
    from dse_validation.preview.argocd import build_manifests

    wi = "wi_" + "a" * 64  # 67 chars, como o id real
    cfg = PreviewConfig()
    exp = datetime.now(timezone.utc) + timedelta(seconds=600)
    m = build_manifests("preview-wi-x", wi, "tenant_dev", exp, 600, cfg)

    label_re = re.compile(r'dse\.fintex/work-item:\s+"([^"]*)"')
    for name, manifest in m.items():
        for val in label_re.findall(manifest):
            assert len(val) <= 63, f"label em {name} tem {len(val)} chars (>63)"
    # o id COMPLETO tem que estar preservado na annotation do namespace
    assert f'dse.fintex/work-item-id: "{wi}"' in m["namespace.yaml"]


def test_pr_image_and_app_port_flow_into_deployment():
    cfg = PreviewConfig()
    cfg.app_port = 3000
    m = _manifests(cfg, image="k3d-dse-registry:5510/dse-preview/wallet:abc123")
    assert "image: k3d-dse-registry:5510/dse-preview/wallet:abc123" in m["deployment.yaml"]
    assert "containerPort: 3000" in m["deployment.yaml"]
    assert "targetPort: 3000" in m["service.yaml"]  # Service publica 80 → app_port


def test_build_pr_image_fail_safe_reasons(tmp_path, monkeypatch):
    from dse_validation.preview.pr_image import build_pr_image
    cfg = PreviewConfig()
    # flag desligada → placeholder
    cfg.build_image = False
    ref, reason, port = build_pr_image(work_item_id="wi-nope", repo="a/b", head_sha="s", cfg=cfg)
    assert ref is None and reason == "build_disabled" and port is None
    # ligada mas sem workspace → placeholder com motivo
    cfg.build_image = True
    monkeypatch.setenv("DSE_SANDBOX_STATE_DIR", str(tmp_path))
    ref, reason, port = build_pr_image(work_item_id="wi-nope", repo="a/b", head_sha="s", cfg=cfg)
    assert ref is None and reason.startswith("workspace_not_found")
    # workspace sem Dockerfile E sem package.json → placeholder (não é Node)
    ws = tmp_path / "wi-nodf" / "workspace"
    ws.mkdir(parents=True)
    ref, reason, port = build_pr_image(work_item_id="wi-nodf", repo="a/b", head_sha="s", cfg=cfg)
    assert ref is None and reason == "no_dockerfile_and_not_node" and port is None


def test_synthesize_node_dockerfile_for_app_without_dockerfile(tmp_path):
    """Decisão do operador (2026-07-22): app Node sem Dockerfile → o DSE
    sintetiza um Dockerfile padrão (não toca no repo) e detecta a porta 3000."""
    import json as _json
    from dse_validation.preview.pr_image import _synthesize_node_dockerfile, _DEFAULT_NODE_PORT

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text(_json.dumps({
        "type": "module", "main": "server.js",
        "scripts": {"start": "node server.js"}, "dependencies": {},
    }))
    path = _synthesize_node_dockerfile(str(ws), _DEFAULT_NODE_PORT)
    assert path is not None
    content = open(path).read()
    assert "FROM node:22-alpine" in content
    assert f"ENV PORT={_DEFAULT_NODE_PORT}" in content
    assert f"EXPOSE {_DEFAULT_NODE_PORT}" in content
    assert 'CMD ["npm", "start"]' in content
    # arquivo TEMPORÁRIO — fora do workspace (não polui o git da tarefa)
    assert str(ws) not in path
    import os as _os
    _os.remove(path)


def test_synthesize_falls_back_to_node_main_without_start_script(tmp_path):
    import json as _json
    from dse_validation.preview.pr_image import _synthesize_node_dockerfile, _DEFAULT_NODE_PORT

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text(_json.dumps({"main": "app.js", "scripts": {}}))
    path = _synthesize_node_dockerfile(str(ws), _DEFAULT_NODE_PORT)
    assert path is not None
    assert 'CMD ["node", "app.js"]' in open(path).read()
    import os as _os
    _os.remove(path)


def test_synthesize_returns_none_when_not_node(tmp_path):
    from dse_validation.preview.pr_image import _synthesize_node_dockerfile, _DEFAULT_NODE_PORT
    ws = tmp_path / "ws"
    ws.mkdir()  # sem package.json
    assert _synthesize_node_dockerfile(str(ws), _DEFAULT_NODE_PORT) is None


# ---------------------------------------------------------------------------
# D1 — link do preview NA DESCRIÇÃO do PR (não como comentário, nem só na issue)
# ---------------------------------------------------------------------------
_PR_BODY_BASE = (
    "### Fintex DSE — automatically generated PR\n\n"
    "- **WorkItem**: `wi_x`\n"
    "- **Risk class**: `medium`\n"
    "- **Summary**: corrige o bug\n"
    "- **Test evidence (L1)**: (no evidence link)\n\n"
    "Closes #2\n"
)


class _FakePrBodyClient:
    def __init__(self, body):
        self.body = body
    def get_pull_request(self, repo, n):
        return {"number": n, "state": "open", "body": self.body}
    def update_pull_request(self, repo, n, *, body):
        self.body = body


def test_preview_link_written_into_pr_body_after_evidence(monkeypatch):
    """Pedido do operador (2026-07-22): o link do preview na DESCRIÇÃO do PR,
    como `- **Preview**: <url>`, logo após a bala de evidência L1."""
    import dse_validation.preview.argocd as arg
    import dse_validation.github.client as gc

    fake = _FakePrBodyClient(_PR_BODY_BASE)
    monkeypatch.setattr(gc, "build_github_client", lambda cfg=None: fake)
    inp = TriggerPreviewInput(
        work_item_id="wi_x", tenant_id="t", repo="a/b", pr_number=11,
        files_changed=["src/store.js"],
    )
    url = "http://preview-x.preview.localhost:8081"
    arg._put_preview_link_in_pr_body(inp, url, "deployable", actor="system:test")
    body = fake.body
    assert f"- **Preview**: {url}" in body
    # posicionado logo após a evidência L1
    ev = body.index("- **Test evidence (L1)**:")
    pv = body.index("- **Preview**:")
    assert ev < pv


def test_preview_link_in_body_is_idempotent(monkeypatch):
    """Re-disparo (fix cycle) REESCREVE a mesma linha — não duplica."""
    import dse_validation.preview.argocd as arg
    import dse_validation.github.client as gc

    fake = _FakePrBodyClient(_PR_BODY_BASE)
    monkeypatch.setattr(gc, "build_github_client", lambda cfg=None: fake)
    inp = TriggerPreviewInput(
        work_item_id="wi_x", tenant_id="t", repo="a/b", pr_number=11,
        files_changed=["src/store.js"],
    )
    arg._put_preview_link_in_pr_body(inp, "http://old.localhost", "deployable", actor="t")
    arg._put_preview_link_in_pr_body(inp, "http://new.localhost", "deployable", actor="t")
    assert fake.body.count("- **Preview**:") == 1  # uma linha só
    assert "http://new.localhost" in fake.body and "http://old.localhost" not in fake.body


def test_preview_link_body_noop_without_pr_number(monkeypatch):
    import dse_validation.preview.argocd as arg
    import dse_validation.github.client as gc
    fake = _FakePrBodyClient(_PR_BODY_BASE)
    monkeypatch.setattr(gc, "build_github_client", lambda cfg=None: fake)
    inp = TriggerPreviewInput(
        work_item_id="wi_x", tenant_id="t", repo="a/b", pr_number=0,
        files_changed=["src/store.js"],
    )
    arg._put_preview_link_in_pr_body(inp, "http://x", "deployable", actor="t")
    assert fake.body == _PR_BODY_BASE  # pr_number=0 → não toca no corpo


# ---------------------------------------------------------------------------
# 1b. caps de concorrência por tenant (ADR-26, dia 1)
# ---------------------------------------------------------------------------
def test_concurrency_cap_evicts_oldest_lru(work_item_id, tenant_id, tmp_path):
    """Cap cheio => eviction LRU: o preview mais ANTIGO cede o slot para o PR
    novo (decisão operador 2026-07-23). O gate do cap deixa de degradar quando
    há o que evictar — aqui o fluxo passa do cap e degrada só DEPOIS, no
    provisionamento (kube_context inexistente), provando a liberação da vaga."""
    db.set_preview_cap(tenant_id, 2)
    # duas linhas "created" ativas do MESMO tenant (contagem real no Postgres);
    # -0 é a mais antiga (created_at menor) — a candidata à eviction.
    for i in range(2):
        db.upsert_preview(
            work_item_id=f"{work_item_id}-{i}", tenant_id=tenant_id, pr_number=i,
            repo="acme/app", status="created", namespace=f"preview-x-{i}",
        )
    assert db.count_active_previews(tenant_id) == 2
    cfg = PreviewConfig()
    cfg.repo_dir = str(tmp_path / "preview-repo")  # gitops real em repo temporário
    cfg.kube_context = "k3d-cluster-que-nao-existe"
    ref = trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/app",
            pr_number=12, files_changed=["frontend/app.tsx"],
        ),
        cfg=cfg,
    )
    # o mais antigo foi reaped (slot liberado); o mais novo continua ativo
    assert db.get_preview(f"{work_item_id}-0")["status"] == "reaped"
    assert db.get_preview(f"{work_item_id}-1")["status"] == "created"
    # e o fluxo PASSOU do gate do cap (degraded aqui vem do cluster inexistente)
    assert ref.status == "degraded"
    assert "cap" not in ref.detail


def test_concurrency_cap_degrades_when_eviction_fails(work_item_id, tenant_id, tmp_path, monkeypatch):
    """Se a remoção GitOps falhar, a eviction é best-effort e o gate degrada
    como antes (failure mode 9 — preview nunca bloqueia o PR)."""
    db.set_preview_cap(tenant_id, 1)
    db.upsert_preview(
        work_item_id=f"{work_item_id}-old", tenant_id=tenant_id, pr_number=1,
        repo="acme/app", status="created", namespace="preview-x-old",
    )
    from dse_validation.preview import gitops as gitops_mod

    def _boom(repo_dir, name):
        raise RuntimeError("git indisponível")

    # argocd resolve `gitops.remove_preview_dir` na chamada — patch no módulo basta.
    monkeypatch.setattr(gitops_mod, "remove_preview_dir", _boom)
    ref = trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/app",
            pr_number=14, files_changed=["frontend/app.tsx"],
        )
    )
    assert ref.status == "degraded"
    assert "cap" in ref.detail
    # o antigo continua ativo — nada foi marcado reaped sem remoção real
    assert db.get_preview(f"{work_item_id}-old")["status"] == "created"


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
def _k3d_cluster_available() -> bool:
    """Exige a capacidade COMPLETA: cluster k3d ativo E Argo CD instalado
    (o teste materializa uma Application de verdade). No runner CI não há
    cluster; num k3d sem Argo CD também pula — a prova roda só no ambiente
    completo (dev/VPS com Argo CD)."""
    import shutil
    import subprocess

    if shutil.which("kubectl") is None:
        return False
    ctx = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True)
    if ctx.returncode != 0 or not ctx.stdout.strip().startswith("k3d-"):
        return False
    argo = subprocess.run(
        ["kubectl", "get", "namespace", "argocd"], capture_output=True, text=True
    )
    return argo.returncode == 0


@pytest.mark.skipif(not _k3d_cluster_available(), reason="requer cluster k3d ativo (Argo CD)")
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
