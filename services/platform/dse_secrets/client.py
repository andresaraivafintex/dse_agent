"""Cliente fino sobre a API HTTP do Vault (WSF-E2-T3a).

Contrato estável, publicado para consumo cross-workstream (WS-A/WS-C/WS-D
devem importar isto para ler webhook secrets, tokens de serviço e
credenciais de provider em vez de env vars em texto plano):

    from dse_secrets import get_secret, put_secret

    creds = get_secret("dse/slack/webhook")          # -> dict
    put_secret("dse/slack/webhook", {"signing_secret": "..."})

Assinatura estável de `SecretsClient`:

    SecretsClient(vault_addr: str | None = None, token: str | None = None,
                  mount_point: str = "secret")
        .get_secret(path: str) -> dict[str, Any]
        .put_secret(path: str, data: dict[str, Any]) -> None
        .delete_secret(path: str) -> None   # soft-delete (KV v2), auditável

Configuração via env var (NUNCA hardcode token em código/config versionado):
  - VAULT_ADDR         (default: http://localhost:8200 — dev local)
  - VAULT_TOKEN        (obrigatório em produção; dev local pode usar
                         VAULT_DEV_ROOT_TOKEN=dse_dev_root só para smoke-test
                         local, nunca em manifest/CI real)
  - VAULT_KV_MOUNT     (default: "secret" — mount KV v2 que o Vault dev sobe
                         por padrão; produção deve usar um mount dedicado por
                         ambiente, ex. "dse-prod")

Backend: usa `hvac` quando disponível (dependência opcional); cai para
`requests` puro contra a API HTTP do Vault caso `hvac` não esteja instalado
— nenhuma lógica de negócio depende de qual dos dois está em uso.

Cada leitura/escrita de secret é uma decisão consequente de segurança (P8):
o caller (adapter/serviço) é responsável por chamar `dse_audit.emit(...)`
ao redor de puts de credenciais novas/rotacionadas — este cliente não decide
sozinho o que auditar porque não conhece o `tenant_id`/`work_item_id` do
contexto de chamada (mantém P1: nenhuma decisão de fluxo aqui, só I/O).
"""
from __future__ import annotations

import os
from typing import Any

try:
    import hvac  # type: ignore

    _HAS_HVAC = True
except ImportError:  # pragma: no cover - exercised only when hvac absent
    _HAS_HVAC = False

import requests


class VaultUnavailableError(RuntimeError):
    """Levantado quando o Vault não responde ou nega a operação (token
    inválido/expirado, path fora de política). Nunca engolir silenciosamente
    — decline-never-truncate (P6): falha limpa e visível."""


class SecretsClient:
    def __init__(
        self,
        vault_addr: str | None = None,
        token: str | None = None,
        mount_point: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.vault_addr = vault_addr or os.environ.get("VAULT_ADDR", "http://localhost:8200")
        self.token = token or os.environ.get("VAULT_TOKEN") or os.environ.get(
            "VAULT_DEV_ROOT_TOKEN"
        )
        self.mount_point = mount_point or os.environ.get("VAULT_KV_MOUNT", "secret")
        self.timeout = timeout

        if not self.token:
            raise VaultUnavailableError(
                "Nenhum token do Vault configurado. Defina VAULT_TOKEN (produção) ou "
                "VAULT_DEV_ROOT_TOKEN (dev local apenas) como env var — nunca hardcode."
            )

        self._hvac_client = None
        if _HAS_HVAC:
            self._hvac_client = hvac.Client(url=self.vault_addr, token=self.token, timeout=self.timeout)

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------
    def get_secret(self, path: str) -> dict[str, Any]:
        """Lê um secret KV v2 em `path` (sem o prefixo do mount point).
        Retorna o dict de dados (`data.data` do payload KV v2). Levanta
        `VaultUnavailableError` se o path não existir, o token for inválido,
        ou o Vault estiver inacessível."""
        if self._hvac_client is not None:
            try:
                resp = self._hvac_client.secrets.kv.v2.read_secret_version(
                    path=path, mount_point=self.mount_point, raise_on_deleted_version=True
                )
                return resp["data"]["data"]
            except Exception as exc:  # hvac.exceptions.* ou erro de rede
                raise VaultUnavailableError(f"falha ao ler secret '{path}': {exc}") from exc

        return self._get_secret_raw(path)

    def put_secret(self, path: str, data: dict[str, Any]) -> None:
        """Escreve (nova versão de) um secret KV v2 em `path`."""
        if self._hvac_client is not None:
            try:
                self._hvac_client.secrets.kv.v2.create_or_update_secret(
                    path=path, secret=data, mount_point=self.mount_point
                )
                return
            except Exception as exc:
                raise VaultUnavailableError(f"falha ao escrever secret '{path}': {exc}") from exc

        self._put_secret_raw(path, data)

    def delete_secret(self, path: str) -> None:
        """Soft-delete da versão mais recente (KV v2 preserva histórico —
        auditável via `vault kv metadata` mesmo depois do delete)."""
        if self._hvac_client is not None:
            try:
                self._hvac_client.secrets.kv.v2.delete_latest_version_of_secret(
                    path=path, mount_point=self.mount_point
                )
                return
            except Exception as exc:
                raise VaultUnavailableError(f"falha ao deletar secret '{path}': {exc}") from exc

        url = f"{self.vault_addr}/v1/{self.mount_point}/data/{path}"
        resp = requests.delete(url, headers=self._headers(), timeout=self.timeout)
        if resp.status_code not in (200, 204):
            raise VaultUnavailableError(f"delete '{path}' -> HTTP {resp.status_code}: {resp.text}")

    # ------------------------------------------------------------------
    # Backend requests puro (sem hvac instalado)
    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"X-Vault-Token": self.token}

    def _get_secret_raw(self, path: str) -> dict[str, Any]:
        url = f"{self.vault_addr}/v1/{self.mount_point}/data/{path}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            raise VaultUnavailableError(f"Vault inacessível em {self.vault_addr}: {exc}") from exc

        if resp.status_code == 404:
            raise VaultUnavailableError(f"secret '{path}' não encontrado (404)")
        if resp.status_code != 200:
            raise VaultUnavailableError(f"GET '{path}' -> HTTP {resp.status_code}: {resp.text}")

        payload = resp.json()
        return payload["data"]["data"]

    def _put_secret_raw(self, path: str, data: dict[str, Any]) -> None:
        url = f"{self.vault_addr}/v1/{self.mount_point}/data/{path}"
        try:
            resp = requests.post(url, headers=self._headers(), json={"data": data}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise VaultUnavailableError(f"Vault inacessível em {self.vault_addr}: {exc}") from exc

        if resp.status_code not in (200, 204):
            raise VaultUnavailableError(f"PUT '{path}' -> HTTP {resp.status_code}: {resp.text}")


# ---------------------------------------------------------------------------
# Conveniência em nível de módulo — a maioria dos callers só precisa disto.
# Constrói um cliente default (env vars) por chamada; para uso intensivo em
# um serviço de longa duração, instancie `SecretsClient` uma vez e reutilize.
# ---------------------------------------------------------------------------
def get_secret(path: str) -> dict[str, Any]:
    return SecretsClient().get_secret(path)


def put_secret(path: str, data: dict[str, Any]) -> None:
    SecretsClient().put_secret(path, data)
