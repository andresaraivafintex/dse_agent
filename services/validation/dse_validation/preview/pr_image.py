"""Plano 08 §D (D4) — imagem REAL do PR para o preview.

Quando `PreviewConfig.build_image` está ligado e o workspace da tarefa tem um
`Dockerfile`, builda a imagem do HEAD do PR e a pusha para o registry local do
cluster (k3d): o preview passa a servir o APP do PR, não o placeholder nginx —
o revisor humano clica no link e decide sobre a mudança de verdade.

Resolução do workspace: o caminho canônico já existente
(`MergeBaseConfig.locations` → $DSE_SANDBOX_STATE_DIR/<wi>/workspace) — zero
mudança de contrato. O `docker build/push` roda via o socket do host (o worker
já o monta para os sandboxes — docker-outside-of-docker).

Fail-safe SEMPRE (failure mode 9 do preview): sem Dockerfile, sem workspace,
build/push falhou, flag desligada → retorna None e o caller usa o placeholder,
com o motivo auditado (P8). O build nunca bloqueia nem derruba o preview.

Referências de registry (k3d):
  push_ref = <registry_push>/dse-preview/<repo-slug>:<tag>   (daemon do host: localhost:5510)
  pull_ref = <registry_pull>/dse-preview/<repo-slug>:<tag>   (nodes do cluster: k3d-dse-registry:5510)
Mesmo storage, dois nomes — padrão do k3d registry.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile

from dse_validation.config import PreviewConfig
from dse_validation.merge_base import MergeBaseConfig

logger = logging.getLogger("dse_validation.preview.pr_image")

# Porta convencional de app Node quando não há Dockerfile. O app respeita
# `process.env.PORT` (padrão Express/Node), então o Dockerfial sintetizado seta
# ENV PORT e EXPOSE nesta porta — e o preview usa a mesma como targetPort.
_DEFAULT_NODE_PORT = 3000


def _synthesize_node_dockerfile(workspace: str, port: int) -> str | None:
    """Gera um Dockerfile PADRÃO para um app Node sem Dockerfile próprio, num
    arquivo TEMPORÁRIO fora do workspace (não polui o git da tarefa). Retorna o
    caminho, ou None se não parecer um app Node (sem package.json com `start`).

    Decisão do operador (2026-07-22): o DSE containeriza apps Node por conta
    própria — o preview mostra o app real sem exigir Dockerfile no repo do
    usuário. Fail-safe: qualquer coisa fora do esperado → None → placeholder."""
    pkg_path = os.path.join(workspace, "package.json")
    if not os.path.isfile(pkg_path):
        return None
    try:
        with open(pkg_path) as fh:
            pkg = json.load(fh)
    except (OSError, ValueError):
        return None
    scripts = pkg.get("scripts") or {}
    if "start" in scripts:
        run_cmd = 'CMD ["npm", "start"]'
    else:
        main = pkg.get("main") or "server.js"
        run_cmd = f'CMD ["node", "{main}"]'
    # npm ci quando há lockfile (reprodutível); senão npm install. `--omit=dev`
    # porque preview roda o app, não os testes. Zero-deps → passo trivial.
    has_lock = os.path.isfile(os.path.join(workspace, "package-lock.json"))
    install = "npm ci --omit=dev || npm install --omit=dev" if has_lock else "npm install --omit=dev || true"
    dockerfile = f"""FROM node:22-alpine
WORKDIR /app
COPY package*.json ./
RUN {install}
COPY . .
ENV PORT={port}
EXPOSE {port}
{run_cmd}
"""
    fd, path = tempfile.mkstemp(prefix="dse-synth-", suffix=".Dockerfile")
    with os.fdopen(fd, "w") as fh:
        fh.write(dockerfile)
    return path


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in value.lower()).strip("-") or "app"


def image_refs(repo: str, tag: str, cfg: PreviewConfig) -> tuple[str, str]:
    name = f"dse-preview/{_slug(repo)}:{_slug(tag)[:40]}"
    return f"{cfg.registry_push}/{name}", f"{cfg.registry_pull}/{name}"


def build_pr_image(
    *,
    work_item_id: str,
    repo: str,
    head_sha: str | None,
    cfg: PreviewConfig | None = None,
) -> tuple[str | None, str, int | None]:
    """Retorna `(pull_ref, motivo, app_port)`. pull_ref=None => usar placeholder
    (motivo diz por quê — vai para o audit/detail, nunca silencioso). app_port é
    a porta detectada quando o DSE sintetiza o Dockerfile (app Node sem
    Dockerfile próprio); None => o caller usa o default do cfg."""
    cfg = cfg or PreviewConfig()
    if not cfg.build_image:
        return None, "build_disabled", None

    _bare, workspace = MergeBaseConfig().locations(work_item_id)
    if not os.path.isdir(workspace):
        return None, f"workspace_not_found:{workspace}", None

    # Estratégia de containerização (decisão do operador 2026-07-22):
    #   1) Dockerfile do repo → build direto (porta = default do cfg / EXPOSE).
    #   2) Sem Dockerfile mas app Node → SINTETIZA um Dockerfile padrão (porta
    #      detectada). O preview mostra o app real sem tocar no repo do usuário.
    #   3) Nada reconhecível → None → placeholder nginx (motivo auditado).
    build_flags: list[str] = []
    app_port: int | None = None
    reason_ok = "pr_image_built"
    synth_path: str | None = None
    if not os.path.isfile(os.path.join(workspace, "Dockerfile")):
        synth_path = _synthesize_node_dockerfile(workspace, _DEFAULT_NODE_PORT)
        if synth_path is None:
            return None, "no_dockerfile_and_not_node", None
        build_flags = ["-f", synth_path]
        app_port = _DEFAULT_NODE_PORT
        reason_ok = "pr_image_built_synthesized_node"

    tag = (head_sha or work_item_id)[:12]
    push_ref, pull_ref = image_refs(repo, tag, cfg)
    try:
        subprocess.run(
            ["docker", "build", *build_flags, "-t", push_ref, "."],
            cwd=workspace, capture_output=True, text=True, check=True,
            timeout=cfg.build_timeout_s,
        )
        subprocess.run(
            ["docker", "push", push_ref],
            capture_output=True, text=True, check=True, timeout=120,
        )
    except FileNotFoundError:
        return None, "docker_cli_unavailable", None
    except subprocess.TimeoutExpired:
        return None, "build_timeout", None
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "")[-300:]
        logger.warning("build da imagem do PR falhou (%s): %s", work_item_id, detail)
        return None, f"build_failed:{detail[:120]}", None
    finally:
        if synth_path:
            try:
                os.remove(synth_path)
            except OSError:
                pass
    return pull_ref, reason_ok, app_port
