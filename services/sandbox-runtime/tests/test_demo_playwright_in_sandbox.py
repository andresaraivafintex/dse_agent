"""WSC-E3-T4b (a)+(c) — ACEITAÇÃO REAL: `npx playwright test --grep @demo`
DENTRO de um container da imagem nova (`dse-sandbox-base:wsc3`) produz vídeo
real.

Nada aqui é simulado: o container é provisionado pelo MESMO
`docker_driver.provision_container` de produção (rootless uid 10001,
`--read-only`, `--cap-drop ALL`, rede interna `dse_sandbox_net` SEM
internet), o fixture `@demo` é materializado pela sessão Tester real (via
toolset — a mesma convenção `demos/<work_item_id>/` do teste de
conformidade), a página estática é SERVIDA localmente dentro do container
(`webServer` do Playwright → `python3 -m http.server`), e o chromium
headless grava o vídeo (.webm — formato nativo do Playwright; transcodificar
para mp4, se a superfície exigir, é pós-processamento do pipeline do WS-E,
documentado em `demo_fixture.py`).

A execução sem internet dentro do sandbox só funciona porque a toolchain
INTEIRA (node + @playwright/test pinado + chromium headless) já está na
imagem — que é exatamente o ponto do T4b.

Se a imagem ainda não existe localmente, o teste a builda de verdade
(uma vez; fica em cache) — nunca skipa silenciosamente.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import docker as docker_sdk
import pytest

from sandbox_runtime import docker_driver
from sandbox_runtime.activities import (
    ProvisionSandboxInput,
    RunTesterTurnInput,
    TeardownSandboxInput,
    _paths_for,
    _run_tester_turn_impl,
    provision_sandbox,
    teardown_sandbox,
)
from sandbox_runtime.demo_fixture import demo_authoring_script

SANDBOX_IMAGE = "dse-sandbox-base:wsc3"
_DOCKERFILE = Path(__file__).resolve().parents[1] / "docker" / "Dockerfile.sandbox-base"


@pytest.fixture(scope="module")
def sandbox_image(docker_client: docker_sdk.DockerClient) -> str:
    try:
        docker_client.images.get(SANDBOX_IMAGE)
    except docker_sdk.errors.ImageNotFound:
        # Build real (P8: o aceite é executável, não asserção). Lento na
        # primeira vez (~min, download do chromium); cacheado depois.
        subprocess.run(
            ["docker", "build", "-f", str(_DOCKERFILE), "-t", SANDBOX_IMAGE, str(_DOCKERFILE.parent)],
            check=True, capture_output=True, text=True, timeout=900,
        )
        docker_client.images.get(SANDBOX_IMAGE)
    return SANDBOX_IMAGE


def _exec(docker_client, container_id: str, cmd: list[str], workdir: str) -> tuple[int, str]:
    c = docker_client.containers.get(container_id)
    rc, output = c.exec_run(cmd, workdir=workdir, demux=False)
    return rc, output.decode(errors="replace")


def test_playwright_demo_inside_sandbox_produces_real_video(
    sandbox_image, docker_client, work_item_id, state_dir
):
    tenant = "tenant-t"
    handle = asyncio.run(
        provision_sandbox(
            ProvisionSandboxInput(
                work_item_id=work_item_id,
                tenant_id=tenant,
                image=sandbox_image,
                # chromium precisa de mais memória/pids/tmp que o default
                # `small` da Fase 1 — caps continuam FINITOS e derivados do
                # budget (nunca ilimitados).
                budget={"resource_class": "large", "tmp_mb": 512},
            )
        )
    )
    try:
        # 1) Tester real autora o fixture @demo na convenção demos/<wi>/.
        asyncio.run(
            _run_tester_turn_impl(
                RunTesterTurnInput(work_item_id=work_item_id, tenant_id=tenant, instruction="autora @demo"),
                authoring_script=demo_authoring_script(work_item_id),
            )
        )
        workspace, _bare = _paths_for(work_item_id)
        demo_dir_in_container = f"/workspace/demos/{work_item_id}"

        # 2) Aceitação literal do T4b: npx playwright test --grep @demo
        #    DENTRO do container da imagem nova.
        rc, out = _exec(
            docker_client,
            handle.container_id,
            ["npx", "playwright", "test", "--grep", "@demo"],
            workdir=demo_dir_in_container,
        )
        assert rc == 0, f"playwright @demo falhou dentro do sandbox (rc={rc}):\n{out}"
        assert "1 passed" in out

        # 3) Vídeo REAL no workspace (bind mount => visível do host).
        results_dir = Path(workspace) / "demos" / work_item_id / "test-results"
        videos = list(results_dir.rglob("*.webm"))
        assert videos, f"nenhum vídeo .webm em {results_dir}"
        assert videos[0].stat().st_size > 10_000, "vídeo suspeito de vazio"
        # trace zip também (vira playwright_trace no artifact store do WS-E)
        assert list(results_dir.rglob("trace.zip")), "trace.zip ausente"

        # 4) O sandbox continua o de produção: rootless e sem internet.
        rc_uid, uid_out = _exec(docker_client, handle.container_id, ["id", "-u"], workdir="/workspace")
        assert rc_uid == 0 and uid_out.strip() != "0"
        container = docker_client.containers.get(handle.container_id)
        nets = container.attrs["NetworkSettings"]["Networks"]
        assert list(nets) == [docker_driver.SANDBOX_NETWORK_NAME]
    finally:
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))
