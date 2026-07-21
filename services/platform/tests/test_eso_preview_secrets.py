"""WSF-E2-T3b(b) — secrets de preview via External Secrets Operator REAL no
cluster k3d `dse-preview` (infra/k8s-local/setup-eso.sh), lendo o Vault REAL
do compose pela rede dse_net. Nada aqui é mockado: o teste prova que um
Secret k8s materializa num namespace de preview a partir do Vault e que uma
rotação no Vault se propaga dentro do refreshInterval.

Skips (com razão explícita, nunca silencioso) se o cluster/ESO não estiverem
no ar — mesmo padrão dos testes adversariais do egress-proxy da Fase 1.
"""
from __future__ import annotations

import base64
import json
import subprocess
import time
import uuid

import pytest
from dse_platform import rotate_secret
from dse_secrets import SecretsClient
from dse_secrets.client import VaultUnavailableError

KUBE_CONTEXT = "k3d-dse-preview"


def _kubectl(*args: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, *args],
        input=input_text, capture_output=True, text=True, check=check, timeout=120,
    )


@pytest.fixture(scope="module")
def cluster():
    try:
        _kubectl("get", "ns", "external-secrets")
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"cluster k3d/ESO indisponível (rode infra/k8s-local/setup-eso.sh): {exc}")
    ready = _kubectl(
        "get", "clustersecretstore", "dse-vault",
        "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}", check=False,
    )
    if ready.stdout.strip() != "True":
        pytest.skip("ClusterSecretStore dse-vault não está Ready — rode setup-eso.sh")
    return True


@pytest.fixture(scope="module")
def vault():
    try:
        client = SecretsClient()
        client.put_secret("dse/preview/eso-smoke", {"ok": "1"})
    except VaultUnavailableError as exc:
        pytest.skip(f"Vault da fundação indisponível: {exc}")
    return client


@pytest.fixture()
def preview_ns(cluster):
    ns = f"dse-preview-test-{uuid.uuid4().hex[:6]}"
    _kubectl("create", "namespace", ns)
    yield ns
    _kubectl("delete", "namespace", ns, "--wait=false", check=False)


def _external_secret_manifest(ns: str, name: str, vault_path: str, properties: list[str]) -> str:
    return json.dumps(
        {
            "apiVersion": "external-secrets.io/v1",
            "kind": "ExternalSecret",
            "metadata": {"name": name, "namespace": ns},
            "spec": {
                "refreshInterval": "5s",
                "secretStoreRef": {"kind": "ClusterSecretStore", "name": "dse-vault"},
                "target": {"name": name, "creationPolicy": "Owner"},
                "data": [
                    {"secretKey": prop, "remoteRef": {"key": vault_path, "property": prop}}
                    for prop in properties
                ],
            },
        }
    )


def _read_k8s_secret_key(ns: str, name: str, key: str) -> str | None:
    res = _kubectl(
        "get", "secret", name, "-n", ns, "-o", f"jsonpath={{.data.{key.replace('.', '\\.')}}}",
        check=False,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return None
    return base64.b64decode(res.stdout.strip()).decode()


def _wait_for(predicate, timeout_s: float, interval_s: float = 2.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval_s)
    return last


def test_eso_deployments_available(cluster):
    out = _kubectl("get", "deployment", "-n", "external-secrets", "-o", "json").stdout
    deployments = json.loads(out)["items"]
    names = {d["metadata"]["name"] for d in deployments}
    assert "external-secrets" in names
    for d in deployments:
        conditions = {c["type"]: c["status"] for c in d["status"].get("conditions", [])}
        assert conditions.get("Available") == "True", f"{d['metadata']['name']} não está Available"


def test_secret_materializes_in_preview_namespace_from_vault(vault, preview_ns):
    """Aceite WSF-E2-T3b(b): o Secret materializa no namespace a partir do
    Vault, e uma rotação no Vault se propaga (secrets de preview 100% via
    ESO — nenhum secret em manifest)."""
    path = f"dse/preview/test-{uuid.uuid4().hex[:8]}"
    marker_v1 = f"v1-{uuid.uuid4().hex}"
    vault.put_secret(path, {"database_url": marker_v1, "api_key": "k1"})

    _kubectl("apply", "-f", "-", input_text=_external_secret_manifest(
        preview_ns, "app-secrets", path, ["database_url", "api_key"]
    ))
    _kubectl("wait", "--for=condition=Ready", "externalsecret/app-secrets",
             "-n", preview_ns, "--timeout=90s")

    value = _wait_for(lambda: _read_k8s_secret_key(preview_ns, "app-secrets", "database_url"), 60)
    assert value == marker_v1, f"Secret k8s não bate com o Vault: {value!r}"

    # rotação REAL via o mesmo caminho do job agendado (WSF-E2-T3b(a)) —
    # o material novo se propaga ao namespace de preview no refresh.
    result = rotate_secret(path, actor="system:secret-rotator", client=vault)
    rotated_value = vault.get_secret(path)["database_url"]
    assert result.new_version == 2

    propagated = _wait_for(
        lambda: (_read_k8s_secret_key(preview_ns, "app-secrets", "database_url") == rotated_value) or None,
        90,
    )
    assert propagated, "rotação no Vault não se propagou ao Secret do preview dentro do prazo"


def test_preview_token_cannot_read_service_secrets(vault, preview_ns):
    """Isolamento do escopo (deny-by-default do Vault): o token do ESO só
    enxerga secret/dse/preview/* — um ExternalSecret apontando para um
    secret de SERVIÇO (dse/service/...) nunca fica Ready nem materializa."""
    service_path = f"dse/service/eso-negative-{uuid.uuid4().hex[:8]}"
    vault.put_secret(service_path, {"token": "not-for-previews"})

    _kubectl("apply", "-f", "-", input_text=_external_secret_manifest(
        preview_ns, "should-never-sync", service_path, ["token"]
    ))
    ready = _kubectl(
        "wait", "--for=condition=Ready", "externalsecret/should-never-sync",
        "-n", preview_ns, "--timeout=30s", check=False,
    )
    assert ready.returncode != 0, "ExternalSecret de path de serviço ficou Ready — policy do Vault furada!"
    assert _read_k8s_secret_key(preview_ns, "should-never-sync", "token") is None
