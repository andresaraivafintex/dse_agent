"""WSE-E4-T10 — trigger_preview: preview environment por PR via Argo CD
ApplicationSet contra o cluster k3d REAL (`k3d-dse-preview`, Argo CD v2.13.3).

Fluxo (implementa a Activity `trigger_preview` do contrato —
`ACTIVITY_TRIGGER_PREVIEW`, input `TriggerPreviewInput`, retorno `PreviewRef`):

  1. Decisão UI-touching por paths-filter DETERMINÍSTICO (FR-20, P1) —
     backend-only => `skipped_backend_only` (conta como sucesso, NUNCA bloqueia).
  2. Cap de previews concorrentes por tenant (ADR-26, dia 1): no cap =>
     `degraded` com detail explícito (não bloqueia o PR; P6 falha limpa).
  3. GitOps: escreve `previews/preview-<work_item_id>/` (Namespace + Deployment
     nginx pinado + Service) no repo de manifests (gitops.py) e garante o
     ApplicationSet `dse-previews` (generator git `previews/*`,
     requeueAfterSeconds baixo). O Argo CD materializa a Application e
     sincroniza => namespace efêmero `preview-<work_item_id>` no cluster.
  4. Espera o Deployment ficar Available (timeout). Falha/timeout =>
     `degraded` (failure mode 9 — o PR nunca fica bloqueado para sempre).
  5. TTL: label/annotation no Namespace + `expires_at` em `wse_previews`.

TTL REAPER — decisão documentada (o adendo prefere kube-janitor, P7):
  kube-janitor deletaria o NAMESPACE no cluster, mas com Argo CD em
  `automated.selfHeal` a fonte da verdade é o GIT — o Argo CD recriaria o
  namespace no próximo reconcile (os dois controllers brigariam). O reaper
  correto em GitOps é remover o diretório do REPO: o ApplicationSet poda a
  Application e o finalizer `resources-finalizer` cascateia a deleção do
  namespace. Por isso `reap_expired_previews()` é um job Python determinístico
  (real, testado contra o cluster) que opera no git — kube-janitor fica
  documentado como upgrade path para recursos NÃO geridos por GitOps.
  A annotation `janitor/ttl` já é gravada no Namespace para esse futuro.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone

from dse_contracts.activities import PreviewRef, TriggerPreviewInput

from dse_validation import db
from dse_validation.config import PreviewConfig
from dse_validation.preview import gitops
from dse_validation.preview.paths_filter import preview_decision

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

logger = logging.getLogger("dse_validation.preview.argocd")


# ---------------------------------------------------------------------------
# kubectl (subprocess, P7 — sem client library extra; kubecontext explícito)
# ---------------------------------------------------------------------------
def _kubectl(cfg: PreviewConfig, args: list[str], *, input_text: str | None = None,
             timeout: int = 60) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["kubectl", "--context", cfg.kube_context, *args],
        input=input_text, capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"kubectl {' '.join(args)} falhou (exit={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc


def namespace_for(work_item_id: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in work_item_id.lower())
    return f"preview-{slug}"[:63].rstrip("-")


# ---------------------------------------------------------------------------
# Manifests do preview mínimo (nginx pinado servindo a página default)
# ---------------------------------------------------------------------------
def build_manifests(namespace: str, work_item_id: str, tenant_id: str,
                    expires_at: datetime, ttl_seconds: int, cfg: PreviewConfig,
                    *, image: str | None = None, app_port: int | None = None) -> dict[str, str]:
    """Plano 08 §D: `image` (D4 — imagem do PR; default placeholder do cfg) e
    `app_port` (porta do app no container; Service/Ingress publicam 80 →
    targetPort). Quando `cfg.external_host_template` está setado, gera também o
    INGRESS (D3) com o hostname derivado do template — o link do PR passa a ser
    clicável de fora (Traefik local / túnel / VPS, mesmo mecanismo)."""
    image = image or cfg.preview_image
    port = app_port or cfg.app_port
    # Label VALUE do k8s tem teto de 63 chars (achado do disparo real: o
    # work_item_id é `wi_`+64 hex = 67 chars → o namespace era REJEITADO pelo
    # Argo, o sync falhava e o namespace nunca nascia → preview degradado sem
    # URL). O id COMPLETO vai na annotation (sem limite de tamanho); a label
    # carrega a versão truncada, que basta para seleção/rótulo.
    wi_label = work_item_id[:63]
    tenant_label = tenant_id[:63]
    labels = (
        f"    app.kubernetes.io/managed-by: dse-preview\n"
        f"    dse.fintex/work-item: \"{wi_label}\"\n"
        f"    dse.fintex/tenant: \"{tenant_label}\"\n"
    )
    ns = f"""apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
  labels:
{labels}  annotations:
    dse.fintex/work-item-id: "{work_item_id}"
    dse.fintex/expires-at: "{expires_at.isoformat()}"
    janitor/ttl: "{ttl_seconds}s"  # upgrade path kube-janitor (ver docstring)
"""
    deploy = f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: preview
  namespace: {namespace}
  labels:
{labels}spec:
  replicas: 1
  selector:
    matchLabels:
      app: preview
  template:
    metadata:
      labels:
        app: preview
    spec:
      containers:
        - name: web
          image: {image}
          ports:
            - containerPort: {port}
          readinessProbe:
            httpGet: {{ path: /, port: {port} }}
            initialDelaySeconds: 1
            periodSeconds: 2
"""
    svc = f"""apiVersion: v1
kind: Service
metadata:
  name: preview
  namespace: {namespace}
  labels:
{labels}spec:
  selector:
    app: preview
  ports:
    - port: 80
      targetPort: {port}
"""
    manifests = {"namespace.yaml": ns, "deployment.yaml": deploy, "service.yaml": svc}

    hostname = cfg.external_hostname_for(namespace)
    if hostname:
        manifests["ingress.yaml"] = f"""apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: preview
  namespace: {namespace}
  labels:
{labels}spec:
  ingressClassName: {cfg.ingress_class}
  rules:
    - host: {hostname}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: preview
                port:
                  number: 80
"""
    return manifests


APPLICATIONSET_TEMPLATE = """apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: {name}
  namespace: {argocd_ns}
spec:
  goTemplate: true
  generators:
    - git:
        repoURL: {repo_url}
        revision: main
        directories:
          - path: previews/*
        requeueAfterSeconds: 15
  template:
    metadata:
      name: "{{{{.path.basename}}}}"
      finalizers:
        - resources-finalizer.argocd.argoproj.io
    spec:
      project: default
      source:
        repoURL: {repo_url}
        targetRevision: main
        path: "{{{{.path.path}}}}"
      destination:
        server: https://kubernetes.default.svc
        namespace: "{{{{.path.basename}}}}"
      syncPolicy:
        automated:
          prune: true
          selfHeal: true
"""


def ensure_applicationset(cfg: PreviewConfig | None = None) -> None:
    """Idempotente: aplica o ApplicationSet `dse-previews` (generator git)."""
    cfg = cfg or PreviewConfig()
    manifest = APPLICATIONSET_TEMPLATE.format(
        name=cfg.applicationset_name,
        argocd_ns=cfg.argocd_namespace,
        repo_url=cfg.repo_url_in_cluster,
    )
    _kubectl(cfg, ["apply", "-f", "-"], input_text=manifest)


_PREVIEW_BODY_MARKER = "<!-- dse:preview -->"
# Linha `- **Preview**: <url>` no corpo do PR, terminada pelo marcador (invisível
# no markdown renderizado) que permite RE-escrever a linha em vez de duplicar.
_PREVIEW_LINE_RE = re.compile(r"^- \*\*Preview\*\*:.*" + re.escape(_PREVIEW_BODY_MARKER) + r"$", re.M)
# ponto de inserção: logo após a bala de evidência L1 do template do PR.
_EVIDENCE_BULLET_PREFIX = "- **Test evidence (L1)**:"


def _preview_body_with_link(body: str, url: str) -> str:
    """Corpo do PR com a linha `- **Preview**: <url>` inserida/atualizada
    (idempotente via marcador). Insere após a bala de evidência L1; se o
    template mudar, acrescenta ao final."""
    line = f"- **Preview**: {url} {_PREVIEW_BODY_MARKER}"
    if _PREVIEW_LINE_RE.search(body):
        return _PREVIEW_LINE_RE.sub(line, body)
    lines = body.splitlines()
    out: list[str] = []
    inserted = False
    for ln in lines:
        out.append(ln)
        if not inserted and ln.startswith(_EVIDENCE_BULLET_PREFIX):
            out.append(line)
            inserted = True
    if not inserted:
        out.append(line)
    return "\n".join(out)


def _put_preview_link_in_pr_body(
    inp: TriggerPreviewInput, url: str | None, kind: str, *, actor: str
) -> None:
    """Escreve o link do preview na DESCRIÇÃO do PR (não como comentário) —
    `- **Preview**: <url>`. Idempotente: re-disparo (fix cycle) reescreve a
    mesma linha. Best-effort — qualquer falha só vira warning; o preview já
    está 'created'."""
    if not url or not inp.pr_number or not inp.repo:
        return
    try:
        from dse_validation.github.client import GitHubConfig, build_github_client

        client = build_github_client(GitHubConfig())
        pr = client.get_pull_request(inp.repo, int(inp.pr_number))
        if pr is None:
            return
        new_body = _preview_body_with_link(pr.get("body") or "", url)
        if new_body == (pr.get("body") or ""):
            return  # nada mudou (já estava com a mesma URL)
        client.update_pull_request(inp.repo, int(inp.pr_number), body=new_body)
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="preview_link_in_pr_body", tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"pr_number": inp.pr_number, "url": url},
            )
    except Exception as exc:  # noqa: BLE001 — best-effort; o preview já está criado
        logger.warning("escrever preview no corpo do PR falhou (%s): %.200s", inp.work_item_id, exc)


def _wait_deployment_available(cfg: PreviewConfig, namespace: str, timeout_s: int) -> None:
    # o namespace só passa a existir depois do sync do Argo CD — espera em TRÊS
    # etapas: namespace criado, deployment CRIADO, deployment Available.
    # A etapa do meio é essencial (achado da prova D3/D4): `kubectl wait
    # --for=condition=...` com um NOME específico falha NA HORA se o recurso
    # ainda não existe — e o Argo aplica namespace→deployment com um gap de
    # segundos no primeiro sync (flake de timing, não de lógica).
    _kubectl(
        cfg,
        ["wait", "--for=create", f"namespace/{namespace}", f"--timeout={timeout_s}s"],
        timeout=timeout_s + 15,
    )
    _kubectl(
        cfg,
        ["wait", "-n", namespace, "--for=create", "deployment/preview",
         f"--timeout={timeout_s}s"],
        timeout=timeout_s + 15,
    )
    _kubectl(
        cfg,
        ["wait", "-n", namespace, "--for=condition=Available", "deployment/preview",
         f"--timeout={timeout_s}s"],
        timeout=timeout_s + 15,
    )


# ---------------------------------------------------------------------------
# trigger_preview — core da Activity do contrato
# ---------------------------------------------------------------------------
def trigger_preview_core(
    inp: TriggerPreviewInput,
    *,
    cfg: PreviewConfig | None = None,
    ttl_seconds: int | None = None,
    actor: str = "system:validation",
) -> PreviewRef:
    cfg = cfg or PreviewConfig()
    ttl = ttl_seconds or cfg.default_ttl_seconds

    # 0) Plano 08 §D — gate operator-set (repo_bindings.deploys_preview). Repo
    # não marcado como "gera preview" pula LIMPO (nunca bloqueia). Distinto de
    # backend-only: aqui o operador declarou que o repo não tem preview.
    if not inp.preview_enabled:
        db.upsert_preview(
            work_item_id=inp.work_item_id, tenant_id=inp.tenant_id,
            pr_number=inp.pr_number, repo=inp.repo, status="skipped_disabled",
            detail="repo não marcado deploys_preview (plano 08 §D)", ttl_seconds=ttl,
        )
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="preview_skipped_disabled",
                tenant_id=inp.tenant_id, work_item_id=inp.work_item_id,
                details={"pr_number": inp.pr_number, "repo": inp.repo},
            )
        return PreviewRef(
            work_item_id=inp.work_item_id, pr_number=inp.pr_number,
            status="skipped_disabled", detail="repo sem preview (deploys_preview=false)",
        )

    # 1) FR-20 + plano 08 §D — paths-filter determinístico (P1). Preview vale se
    # a mudança toca UI (front) OU um serviço deployável (back). Só docs/teste
    # → skipped_backend_only, que conta como SUCESSO e NUNCA bloqueia.
    kind, matched = preview_decision(inp.files_changed, inp.ui_path_globs, inp.deployable_globs)
    if kind == "none":
        db.upsert_preview(
            work_item_id=inp.work_item_id, tenant_id=inp.tenant_id,
            pr_number=inp.pr_number, repo=inp.repo, status="skipped_backend_only",
            detail="nenhum arquivo casa ui/deployable globs (FR-20 + §D)", ttl_seconds=ttl,
        )
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="preview_skipped_backend_only",
                tenant_id=inp.tenant_id, work_item_id=inp.work_item_id,
                details={"pr_number": inp.pr_number, "files_changed": inp.files_changed,
                         "ui_path_globs": inp.ui_path_globs, "deployable_globs": inp.deployable_globs},
            )
        return PreviewRef(
            work_item_id=inp.work_item_id, pr_number=inp.pr_number,
            status="skipped_backend_only", detail="sem mudança previewável (docs/teste)",
        )

    ui_files = matched

    # 2) ADR-26 — cap de previews concorrentes por tenant (dia 1).
    cap = db.get_preview_cap(inp.tenant_id)
    if cap is None:
        cap = cfg.default_max_concurrent
    existing = db.get_preview(inp.work_item_id)
    active = db.count_active_previews(inp.tenant_id)
    already_active = existing is not None and existing["status"] == "created" and existing["reaped_at"] is None
    if not already_active and active >= cap and cap > 0:
        # Eviction LRU (decisão operador 2026-07-23): cap cheio => o preview
        # mais ANTIGO cede o slot para o PR novo (recência vence; o cap segue
        # sendo o teto duro de simultâneos). cap == 0 continua significando
        # "tenant sem previews" — nada a evictar. Falha na remoção NÃO derruba
        # o fluxo aqui: cai no degraded logo abaixo (failure mode 9).
        try:
            for row in db.list_oldest_active_previews(inp.tenant_id, limit=active - cap + 1):
                old_ns = row["namespace"] or namespace_for(row["work_item_id"])
                gitops.remove_preview_dir(cfg.repo_dir, old_ns)
                db.mark_preview_reaped(row["work_item_id"])
                if audit_emit is not None:
                    audit_emit(
                        actor=actor, action="preview_evicted_lru", tenant_id=inp.tenant_id,
                        work_item_id=row["work_item_id"],
                        details={"namespace": old_ns, "evicted_for": inp.work_item_id,
                                 "pr_number": inp.pr_number, "cap": cap},
                    )
        except Exception as exc:  # noqa: BLE001 — eviction é best-effort; degraded decide abaixo
            logger.warning("eviction LRU de preview falhou (%s: %s)", type(exc).__name__, exc)
        active = db.count_active_previews(inp.tenant_id)
    if not already_active and active >= cap:
        detail = f"cap de previews concorrentes do tenant atingido ({active}/{cap}, ADR-26)"
        db.upsert_preview(
            work_item_id=inp.work_item_id, tenant_id=inp.tenant_id,
            pr_number=inp.pr_number, repo=inp.repo, status="degraded",
            detail=detail, ttl_seconds=ttl,
        )
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="preview_degraded", tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"pr_number": inp.pr_number, "reason": "concurrency_cap", "active": active, "cap": cap},
            )
        return PreviewRef(
            work_item_id=inp.work_item_id, pr_number=inp.pr_number,
            status="degraded", detail=detail,
        )

    # 3-4) GitOps + Argo CD contra o cluster real. Qualquer falha => degraded
    # (failure mode 9 — preview nunca bloqueia o PR para sempre).
    namespace = namespace_for(inp.work_item_id)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

    # D4 — imagem REAL do PR quando habilitado (fail-safe: None => placeholder,
    # motivo auditado; o build nunca degrada o preview).
    from dse_validation.preview.pr_image import build_pr_image
    pr_image, image_reason, detected_port = build_pr_image(
        work_item_id=inp.work_item_id, repo=inp.repo, head_sha=inp.head_sha, cfg=cfg,
    )
    # Porta detectada na síntese (app Node) vence o default do cfg — senão o
    # readiness/Service apontariam para a porta errada e o preview degradaria.
    app_port = detected_port or cfg.app_port

    try:
        manifests = build_manifests(namespace, inp.work_item_id, inp.tenant_id, expires_at, ttl, cfg,
                                    image=pr_image, app_port=app_port)
        gitops.write_preview_dir(cfg.repo_dir, namespace, manifests)
        ensure_applicationset(cfg)
        _wait_deployment_available(cfg, namespace, cfg.sync_timeout_s)
    except Exception as exc:  # degraded, nunca bloqueia (failure mode 9)
        detail = f"preview degradado: {type(exc).__name__}: {exc}"
        logger.warning("trigger_preview %s: %s", inp.work_item_id, detail)
        db.upsert_preview(
            work_item_id=inp.work_item_id, tenant_id=inp.tenant_id,
            pr_number=inp.pr_number, repo=inp.repo, status="degraded",
            namespace=namespace, detail=detail[:900], ttl_seconds=ttl, expires_at=expires_at,
        )
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="preview_degraded", tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"pr_number": inp.pr_number, "reason": "provision_failure", "error": detail[:500]},
            )
        return PreviewRef(
            work_item_id=inp.work_item_id, pr_number=inp.pr_number,
            status="degraded", namespace=namespace, detail=detail[:900],
        )

    # plano 08 §D (D3): URL externa (browser-reachable) quando configurada;
    # senão o DNS interno do cluster (link aparece no PR mesmo assim — D1).
    url = cfg.preview_url_for(namespace)
    db.upsert_preview(
        work_item_id=inp.work_item_id, tenant_id=inp.tenant_id,
        pr_number=inp.pr_number, repo=inp.repo, status="created",
        namespace=namespace, url=url, ttl_seconds=ttl, expires_at=expires_at,
        detail=f"{kind} files: {', '.join(ui_files[:10])} | image={image_reason}",
    )
    # D1 (achado do disparo real: o link só ia para o comentário de status na
    # ISSUE de origem; o revisor humano abre o PR e não o via). Escreve o link
    # na DESCRIÇÃO do PR (`- **Preview**: <url>`). Best-effort: nunca derruba o
    # preview (o audit/ledger é a verdade).
    _put_preview_link_in_pr_body(inp, url, kind, actor=actor)
    if audit_emit is not None:
        audit_emit(
            actor=actor, action="preview_created", tenant_id=inp.tenant_id,
            work_item_id=inp.work_item_id,
            details={"pr_number": inp.pr_number, "namespace": namespace, "url": url,
                     "kind": kind, "ttl_seconds": ttl, "expires_at": expires_at.isoformat(),
                     "image": pr_image or cfg.preview_image, "image_source": image_reason,
                     "files": ui_files[:20]},
        )
    return PreviewRef(
        work_item_id=inp.work_item_id, pr_number=inp.pr_number,
        status="created", namespace=namespace, url=url, kind=kind,
    )


# ---------------------------------------------------------------------------
# TTL reaper (job Python determinístico — ver decisão na docstring do módulo)
# ---------------------------------------------------------------------------
def reap_expired_previews(
    *, cfg: PreviewConfig | None = None, actor: str = "system:validation", now=None
) -> list[str]:
    """Remove do GIT os previews expirados (`expires_at <= now`) — o
    ApplicationSet poda a Application e o finalizer cascateia a deleção do
    namespace. Marca `reaped` em wse_previews + audit. Idempotente."""
    cfg = cfg or PreviewConfig()
    reaped: list[str] = []
    for row in db.list_expired_previews(now):
        namespace = row["namespace"] or namespace_for(row["work_item_id"])
        gitops.remove_preview_dir(cfg.repo_dir, namespace)
        db.mark_preview_reaped(row["work_item_id"])
        reaped.append(row["work_item_id"])
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="preview_reaped", tenant_id=row["tenant_id"],
                work_item_id=row["work_item_id"], details={"namespace": namespace},
            )
    return reaped


def wait_namespace_gone(namespace: str, *, cfg: PreviewConfig | None = None, timeout_s: int = 180) -> None:
    """Espera o cascade delete do Argo CD concluir (para testes/verificação)."""
    cfg = cfg or PreviewConfig()
    _kubectl(
        cfg,
        ["wait", "--for=delete", f"namespace/{namespace}", f"--timeout={timeout_s}s"],
        timeout=timeout_s + 15,
    )


def get_preview_http_status(namespace: str, *, cfg: PreviewConfig | None = None, timeout_s: int = 30) -> int:
    """Prova 'URL respondendo' de fora do cluster: `kubectl run curl` efêmero
    dentro do namespace batendo no Service — retorna o status HTTP."""
    cfg = cfg or PreviewConfig()
    proc = _kubectl(
        cfg,
        ["run", "curl-probe", "-n", namespace, "--rm", "-i", "--restart=Never",
         "--image=curlimages/curl:8.10.1", "--command", "--",
         "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         f"http://preview.{namespace}.svc.cluster.local/"],
        timeout=timeout_s + 60,
    )
    digits = "".join(ch for ch in proc.stdout if ch.isdigit())
    return int(digits[:3]) if digits else 0


def _applicationset_status(cfg: PreviewConfig | None = None) -> dict:
    cfg = cfg or PreviewConfig()
    proc = _kubectl(cfg, ["get", "applicationset", "-n", cfg.argocd_namespace,
                          cfg.applicationset_name, "-o", "json"])
    return json.loads(proc.stdout)
