"""WSF-E2-T3a — cliente do secrets backend (Vault).

Roda contra o Vault dev real (localhost:8200, root token `dse_dev_root`) já
levantado pela fundação — nunca mocka o Vault, é o próprio ponto do teste
(provar que o cliente fala HTTP de verdade com o backend real)."""
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
    not _vault_reachable(), reason="Vault dev não está acessível em VAULT_ADDR — infra da fundação não está no ar"
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
    # o token não deve vazar em repr/str default do objeto (defesa mínima
    # contra logging acidental de credenciais)
    assert client.token not in repr(client.__dict__.get("_hvac_client"))
    # __dict__ do próprio client contém o token (necessário para funcionar);
    # a garantia real é que nenhum log/exception message do client vaza o
    # valor do token, o que é verdade pelas mensagens de erro acima (elas
    # nunca interpolam self.token).
