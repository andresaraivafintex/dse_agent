"""Plan 08 §G — security CONFORMANCE suite for the KubernetesSandboxDriver.

Validates that the generated Pod spec is hardened — WITHOUT needing a cluster
(the `build_pod_manifest` core is pure). The LIVE proof (a Pod running under
gVisor/Kata) depends on the cluster; these tests pin the POSTURE in CI so that
nobody can loosen the spec without breaking here.
"""
from __future__ import annotations

import pytest

from sandbox_runtime.driver import (
    IsolatedStageExecutionUnavailable,
    SandboxProvisionRequest,
    StageExecutionRequest,
)
from sandbox_runtime.k8s_driver import (
    K8sSandboxConfig,
    KubernetesSandboxDriver,
    build_pod_manifest,
)
from dse_contracts import Stage


def _req():
    return SandboxProvisionRequest(
        work_item_id="wi_abc123", tenant_id="tenant_dev", branch="dse/wi_abc123",
        workspace_path="/w", checkpoint_path="/c",
    )


def _pod(**cfg_over):
    cfg = K8sSandboxConfig(**cfg_over)
    return build_pod_manifest(_req(), cfg)


def test_pod_and_container_run_as_nonroot_uid():
    pod = _pod()
    psec = pod["spec"]["securityContext"]
    csec = pod["spec"]["containers"][0]["securityContext"]
    assert psec["runAsNonRoot"] is True and psec["runAsUser"] != 0
    assert csec["runAsNonRoot"] is True and csec["runAsUser"] != 0


def test_no_privilege_escalation_and_caps_dropped():
    csec = _pod()["spec"]["containers"][0]["securityContext"]
    assert csec["allowPrivilegeEscalation"] is False
    assert csec["privileged"] is False
    assert csec["capabilities"]["drop"] == ["ALL"]


def test_readonly_rootfs_with_writable_scratch():
    pod = _pod()
    c = pod["spec"]["containers"][0]
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    mounts = {m["name"] for m in c["volumeMounts"]}
    assert {"workspace", "tmp"} <= mounts
    vols = {v["name"]: v for v in pod["spec"]["volumes"]}
    # writable scratch via emptyDir (not hostPath)
    assert "emptyDir" in vols["workspace"] and "emptyDir" in vols["tmp"]


def test_seccomp_runtime_default():
    pod = _pod()
    assert pod["spec"]["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert pod["spec"]["containers"][0]["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"


def test_no_host_namespaces_or_socket():
    spec = _pod()["spec"]
    assert spec["hostNetwork"] is False
    assert spec["hostPID"] is False
    assert spec["hostIPC"] is False
    # no hostPath volume at all (the Docker socket in particular)
    for v in spec["volumes"]:
        assert "hostPath" not in v


def test_service_account_token_not_mounted():
    spec = _pod()["spec"]
    assert spec["automountServiceAccountToken"] is False


def test_egress_routed_through_proxy():
    env = {e["name"]: e["value"] for e in _pod()["spec"]["containers"][0]["env"]}
    assert env["HTTP_PROXY"].startswith("http")
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"]


def test_runtime_class_set_when_configured():
    pod = _pod(runtime_class="gvisor")
    assert pod["spec"]["runtimeClassName"] == "gvisor"


def test_missing_runtime_class_is_flagged_not_silent():
    # weak isolation (no RuntimeClass) is NEVER silent — it marks an annotation
    pod = _pod(runtime_class="")
    assert "runtimeClassName" not in pod["spec"]
    assert any("isolation-warning" in k for k in pod["metadata"]["annotations"])


def test_ephemeral_pod_never_restarts():
    assert _pod()["spec"]["restartPolicy"] == "Never"


def test_resource_limits_present():
    limits = _pod()["spec"]["containers"][0]["resources"]["limits"]
    assert "cpu" in limits and "memory" in limits


def test_driver_advertises_isolated_execution():
    assert KubernetesSandboxDriver().supports_isolated_stage_execution is True


def test_fail_closed_without_kubectl():
    # with no kubectl on PATH, provision/execute FAIL (they never run locally)
    cfg = K8sSandboxConfig(kubectl="kubectl-that-does-not-exist-xyz")
    driver = KubernetesSandboxDriver(cfg)
    with pytest.raises(IsolatedStageExecutionUnavailable):
        driver.execute_stage(StageExecutionRequest(
            sandbox_id="dse-sbx-x", work_item_id="wi", tenant_id="t",
            stage=Stage.coder, input_payload={}, timeout_seconds=5,
        ))
