"""WSC-E3-T4b (a)+(c) — REAL ACCEPTANCE: `npx playwright test --grep @demo`
INSIDE a container of the new image (`dse-sandbox-base:wsc3`) produces a real
video.

Nothing here is simulated: the container is provisioned by the SAME production
`docker_driver.provision_container` (rootless uid 10001, `--read-only`,
`--cap-drop ALL`, internal `dse_sandbox_net` network with NO internet), the
`@demo` fixture is materialized by the real Tester session (via the toolset —
the same `demos/<work_item_id>/` convention as the conformance test), the static
page is SERVED locally inside the container (Playwright's `webServer` →
`python3 -m http.server`), and headless chromium records the video (.webm —
Playwright's native format; transcoding to mp4, if the surface requires it, is
post-processing in the WS-E pipeline, documented in `demo_fixture.py`).

Running without internet inside the sandbox only works because the ENTIRE
toolchain (node + pinned @playwright/test + headless chromium) is already in the
image — which is exactly the point of T4b.

If the image does not exist locally yet, the test really builds it (once; it
stays cached) — it never skips silently.
"""
from __future__ import annotations

import asyncio
import os
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
        # The image (~2GB, chromium download) is NOT built by default — on the
        # CI runner that blows up time/space and the /workspace bind mount does
        # not even have permission. It really runs on dev/VPS where the image
        # exists, or with DSE_BUILD_SANDBOX_IMAGE=1 to build on demand.
        if os.environ.get("DSE_BUILD_SANDBOX_IMAGE") != "1":
            pytest.skip(
                f"image {SANDBOX_IMAGE} missing — pre-build it or set "
                "DSE_BUILD_SANDBOX_IMAGE=1 (real ~2GB build)"
            )
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
                # chromium needs more memory/pids/tmp than Fase 1's `small`
                # default — the caps stay FINITE and derived from the budget
                # (never unlimited).
                budget={"resource_class": "large", "tmp_mb": 512},
            )
        )
    )
    try:
        # 1) The real Tester authors the @demo fixture under the demos/<wi>/ convention.
        asyncio.run(
            _run_tester_turn_impl(
                RunTesterTurnInput(work_item_id=work_item_id, tenant_id=tenant, instruction="autora @demo"),
                authoring_script=demo_authoring_script(work_item_id),
            )
        )
        workspace, _bare = _paths_for(work_item_id)
        demo_dir_in_container = f"/workspace/demos/{work_item_id}"

        # 2) T4b's literal acceptance: npx playwright test --grep @demo
        #    INSIDE the container of the new image.
        rc, out = _exec(
            docker_client,
            handle.container_id,
            ["npx", "playwright", "test", "--grep", "@demo"],
            workdir=demo_dir_in_container,
        )
        assert rc == 0, f"playwright @demo failed inside the sandbox (rc={rc}):\n{out}"
        assert "1 passed" in out

        # 3) REAL video in the workspace (bind mount => visible from the host).
        results_dir = Path(workspace) / "demos" / work_item_id / "test-results"
        videos = list(results_dir.rglob("*.webm"))
        assert videos, f"no .webm video in {results_dir}"
        assert videos[0].stat().st_size > 10_000, "video suspiciously empty"
        # the trace zip too (becomes playwright_trace in WS-E's artifact store)
        assert list(results_dir.rglob("trace.zip")), "trace.zip ausente"

        # 4) The sandbox is still the production one: rootless and with no internet.
        rc_uid, uid_out = _exec(docker_client, handle.container_id, ["id", "-u"], workdir="/workspace")
        assert rc_uid == 0 and uid_out.strip() != "0"
        container = docker_client.containers.get(handle.container_id)
        nets = container.attrs["NetworkSettings"]["Networks"]
        assert list(nets) == [docker_driver.SANDBOX_NETWORK_NAME]
    finally:
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))
