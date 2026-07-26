"""Thin client over the Vault HTTP API (WSF-E2-T3a).

Stable contract, published for cross-workstream consumption (WS-A/WS-C/WS-D
should import this to read webhook secrets, service tokens and provider
credentials instead of plaintext env vars):

    from dse_secrets import get_secret, put_secret

    creds = get_secret("dse/slack/webhook")          # -> dict
    put_secret("dse/slack/webhook", {"signing_secret": "..."})

Stable `SecretsClient` signature:

    SecretsClient(vault_addr: str | None = None, token: str | None = None,
                  mount_point: str = "secret")
        .get_secret(path: str) -> dict[str, Any]
        .put_secret(path: str, data: dict[str, Any]) -> None
        .delete_secret(path: str) -> None   # soft-delete (KV v2), auditable

Configuration via env var (NEVER hardcode a token in versioned code/config):
  - VAULT_ADDR         (default: http://localhost:8200 — local dev)
  - VAULT_TOKEN        (required in production; local dev may use
                         VAULT_DEV_ROOT_TOKEN=dse_dev_root for local smoke tests
                         only, never in a real manifest/CI)
  - VAULT_KV_MOUNT     (default: "secret" — the KV v2 mount the dev Vault brings
                         up by default; production should use a dedicated mount
                         per environment, e.g. "dse-prod")

Backend: uses `hvac` when available (optional dependency); falls back to plain
`requests` against the Vault HTTP API when `hvac` is not installed — no business
logic depends on which of the two is in use.

Every secret read/write is a consequential security decision (P8): the caller
(adapter/service) is responsible for calling `dse_audit.emit(...)` around puts of
new/rotated credentials — this client does not decide on its own what to audit
because it does not know the calling context's `tenant_id`/`work_item_id` (keeps
P1: no flow decisions here, only I/O).
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
    """Raised when Vault does not answer or denies the operation (invalid/expired
    token, path outside policy). Never swallow it silently —
    decline-never-truncate (P6): clean, visible failure."""


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
                "No Vault token configured. Set VAULT_TOKEN (production) or "
                "VAULT_DEV_ROOT_TOKEN (local dev only) as an env var — never hardcode it."
            )

        self._hvac_client = None
        if _HAS_HVAC:
            self._hvac_client = hvac.Client(url=self.vault_addr, token=self.token, timeout=self.timeout)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_secret(self, path: str) -> dict[str, Any]:
        """Reads a KV v2 secret at `path` (without the mount point prefix).
        Returns the data dict (`data.data` of the KV v2 payload). Raises
        `VaultUnavailableError` if the path does not exist, the token is invalid,
        or Vault is unreachable."""
        if self._hvac_client is not None:
            try:
                resp = self._hvac_client.secrets.kv.v2.read_secret_version(
                    path=path, mount_point=self.mount_point, raise_on_deleted_version=True
                )
                return resp["data"]["data"]
            except Exception as exc:  # hvac.exceptions.* or a network error
                raise VaultUnavailableError(f"falha ao ler secret '{path}': {exc}") from exc

        return self._get_secret_raw(path)

    def put_secret(self, path: str, data: dict[str, Any]) -> None:
        """Writes (a new version of) a KV v2 secret at `path`."""
        if self._hvac_client is not None:
            try:
                self._hvac_client.secrets.kv.v2.create_or_update_secret(
                    path=path, secret=data, mount_point=self.mount_point
                )
                return
            except Exception as exc:
                raise VaultUnavailableError(f"failed to write secret '{path}': {exc}") from exc

        self._put_secret_raw(path, data)

    def delete_secret(self, path: str) -> None:
        """Soft-deletes the latest version (KV v2 preserves history — auditable
        via `vault kv metadata` even after the delete)."""
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
    # Plain-requests backend (no hvac installed)
    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"X-Vault-Token": self.token}

    def _get_secret_raw(self, path: str) -> dict[str, Any]:
        url = f"{self.vault_addr}/v1/{self.mount_point}/data/{path}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            raise VaultUnavailableError(f"Vault unreachable at {self.vault_addr}: {exc}") from exc

        if resp.status_code == 404:
            raise VaultUnavailableError(f"secret '{path}' not found (404)")
        if resp.status_code != 200:
            raise VaultUnavailableError(f"GET '{path}' -> HTTP {resp.status_code}: {resp.text}")

        payload = resp.json()
        return payload["data"]["data"]

    def _put_secret_raw(self, path: str, data: dict[str, Any]) -> None:
        url = f"{self.vault_addr}/v1/{self.mount_point}/data/{path}"
        try:
            resp = requests.post(url, headers=self._headers(), json={"data": data}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise VaultUnavailableError(f"Vault unreachable at {self.vault_addr}: {exc}") from exc

        if resp.status_code not in (200, 204):
            raise VaultUnavailableError(f"PUT '{path}' -> HTTP {resp.status_code}: {resp.text}")


# ---------------------------------------------------------------------------
# Module-level convenience — most callers only need this.
# Builds a default client (from env vars) per call; for heavy use inside a
# long-running service, instantiate `SecretsClient` once and reuse it.
# ---------------------------------------------------------------------------
def get_secret(path: str) -> dict[str, Any]:
    return SecretsClient().get_secret(path)


def put_secret(path: str, data: dict[str, Any]) -> None:
    SecretsClient().put_secret(path, data)
