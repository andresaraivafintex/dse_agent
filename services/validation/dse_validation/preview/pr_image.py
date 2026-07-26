"""Plan 08 §D (D4) — the REAL PR image for the preview.

When `PreviewConfig.build_image` is on and the task workspace has a
`Dockerfile`, builds the image from the PR HEAD and pushes it to the cluster's
local registry (k3d): the preview then serves the PR's APP, not the nginx
placeholder — the human reviewer clicks the link and decides about the actual
change.

Workspace resolution: the canonical path that already exists
(`MergeBaseConfig.locations` → $DSE_SANDBOX_STATE_DIR/<wi>/workspace) — zero
contract change. The `docker build/push` runs through the host socket (the
worker already mounts it for the sandboxes — docker-outside-of-docker).

ALWAYS fail-safe (preview failure mode 9): no Dockerfile, no workspace,
build/push failed, flag off → returns None and the caller uses the placeholder,
with the reason audited (P8). The build never blocks nor takes down the preview.

Registry references (k3d):
  push_ref = <registry_push>/dse-preview/<repo-slug>:<tag>   (host daemon: localhost:5510)
  pull_ref = <registry_pull>/dse-preview/<repo-slug>:<tag>   (cluster nodes: k3d-dse-registry:5510)
Same storage, two names — the k3d registry pattern.
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

# Conventional Node app port when there is no Dockerfile. The app honours
# `process.env.PORT` (the Express/Node standard), so the synthesized Dockerfile
# sets ENV PORT and EXPOSE on this port — and the preview uses the same one as
# targetPort.
_DEFAULT_NODE_PORT = 3000


def _synthesize_node_dockerfile(workspace: str, port: int) -> str | None:
    """Generates a STANDARD Dockerfile for a Node app that has no Dockerfile of
    its own, in a TEMPORARY file outside the workspace (does not pollute the
    task's git). Returns the path, or None if it does not look like a Node app
    (no package.json with `start`).

    Operator decision (2026-07-22): the DSE containerizes Node apps on its own —
    the preview shows the real app without requiring a Dockerfile in the user's
    repo. Fail-safe: anything outside the expected → None → placeholder."""
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
    # npm ci when there is a lockfile (reproducible); otherwise npm install.
    # `--omit=dev` because the preview runs the app, not the tests. Zero-deps →
    # trivial step.
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
    """Returns `(pull_ref, reason, app_port)`. pull_ref=None => use the
    placeholder (the reason says why — it goes to the audit/detail, never
    silent). app_port is the port detected when the DSE synthesizes the
    Dockerfile (Node app without its own Dockerfile); None => the caller uses
    the cfg default."""
    cfg = cfg or PreviewConfig()
    if not cfg.build_image:
        return None, "build_disabled", None

    _bare, workspace = MergeBaseConfig().locations(work_item_id)
    if not os.path.isdir(workspace):
        return None, f"workspace_not_found:{workspace}", None

    # Containerization strategy (operator decision 2026-07-22):
    #   1) Repo Dockerfile → direct build (port = cfg default / EXPOSE).
    #   2) No Dockerfile but a Node app → SYNTHESIZES a standard Dockerfile
    #      (detected port). The preview shows the real app without touching the
    #      user's repo.
    #   3) Nothing recognizable → None → nginx placeholder (reason audited).
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
        logger.warning("PR image build failed (%s): %s", work_item_id, detail)
        return None, f"build_failed:{detail[:120]}", None
    finally:
        if synth_path:
            try:
                os.remove(synth_path)
            except OSError:
                pass
    return pull_ref, reason_ok, app_port
