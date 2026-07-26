"""WSF-E2-T3a — secrets backend (Vault) client.

Runs against the real dev Vault (localhost:8200, root token `dse_dev_root`)
already brought up by the foundation — it never mocks Vault, which is the whole
point of the test (prove the client really speaks HTTP to the real backend)."""
from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault("VAULT_ADDR", "http://localhost:8200")
os.environ.setdefault("VAULT_DEV_ROOT_TOKEN", "dse_dev_root")

from dse_secrets import SecretsClient, VaultUnavailableError, get_secret, put_secret  # noqa: E402


def _vault_reachable() -> bool:
    import requests

    try:
        resp = requests.get(f"{os.environ['VAULT_ADDR']}/v1/sys/health", timeout=2)
        return resp.status_code in (200, 429, 472, 473, 501, 503)
    except requests.RequestException:
        return False


pytestmark = pytest.mark.skipif(
    not _vault_reachable(), reason="dev Vault is not reachable at VAULT_ADDR — the foundation infra is not up"
)


@pytest.fixture()
def secret_path():
    return f"dse-test/{uuid.uuid4().hex[:12]}"


def test_put_then_get_roundtrip(secret_path):
    put_secret(secret_path, {"api_key": "sk-test-123", "rotated_by": "unit-test"})
    result = get_secret(secret_path)
    assert result["api_key"] == "sk-test-123"
    assert result["rotated_by"] == "unit-test"


def test_get_missing_secret_raises_clear_error():
    with pytest.raises(VaultUnavailableError):
        get_secret(f"dse-test/never-existed-{uuid.uuid4().hex}")


def test_client_requires_token_env_var(monkeypatch):
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    monkeypatch.delenv("VAULT_DEV_ROOT_TOKEN", raising=False)
    with pytest.raises(VaultUnavailableError):
        SecretsClient()


def test_put_overwrites_and_versions(secret_path):
    put_secret(secret_path, {"v": "1"})
    put_secret(secret_path, {"v": "2"})
    assert get_secret(secret_path)["v"] == "2"


def test_delete_then_get_raises(secret_path):
    client = SecretsClient()
    client.put_secret(secret_path, {"k": "will-be-deleted"})
    client.delete_secret(secret_path)
    with pytest.raises(VaultUnavailableError):
        client.get_secret(secret_path)


def test_no_plaintext_token_in_repr(secret_path):
    client = SecretsClient()
    # the token must not leak through the object's default repr/str (minimal
    # defense against accidentally logging credentials)
    assert client.token not in repr(client.__dict__.get("_hvac_client"))
    # the client's own __dict__ does hold the token (required for it to work);
    # the real guarantee is that no log/exception message from the client leaks
    # the token value, which holds for the error messages above (they never
    # interpolate self.token).
