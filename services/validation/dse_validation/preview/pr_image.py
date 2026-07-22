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

import logging
import os
import subprocess

from dse_validation.config import PreviewConfig
from dse_validation.merge_base import MergeBaseConfig

logger = logging.getLogger("dse_validation.preview.pr_image")


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
) -> tuple[str | None, str]:
    """Retorna `(pull_ref, motivo)`. pull_ref=None => usar placeholder (motivo
    diz por quê — vai para o audit/detail, nunca silencioso)."""
    cfg = cfg or PreviewConfig()
    if not cfg.build_image:
        return None, "build_disabled"

    _bare, workspace = MergeBaseConfig().locations(work_item_id)
    if not os.path.isdir(workspace):
        return None, f"workspace_not_found:{workspace}"
    if not os.path.isfile(os.path.join(workspace, "Dockerfile")):
        return None, "no_dockerfile_in_workspace"

    tag = (head_sha or work_item_id)[:12]
    push_ref, pull_ref = image_refs(repo, tag, cfg)
    try:
        subprocess.run(
            ["docker", "build", "-t", push_ref, "."],
            cwd=workspace, capture_output=True, text=True, check=True,
            timeout=cfg.build_timeout_s,
        )
        subprocess.run(
            ["docker", "push", push_ref],
            capture_output=True, text=True, check=True, timeout=120,
        )
    except FileNotFoundError:
        return None, "docker_cli_unavailable"
    except subprocess.TimeoutExpired:
        return None, "build_timeout"
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "")[-300:]
        logger.warning("build da imagem do PR falhou (%s): %s", work_item_id, detail)
        return None, f"build_failed:{detail[:120]}"
    return pull_ref, "pr_image_built"
