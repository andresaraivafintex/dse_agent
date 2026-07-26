"""WSF-E2-T3b(a) — SCHEDULED rotation of service secrets (ADR-28, complete).

Zero-downtime mechanics: the backend is Vault KV **v2** — each rotation is a
``create_or_update`` that writes a NEW VERSION atomically. An active reader (an
adapter reading the webhook secret, the gateway reading a provider credential)
never observes an "empty path" nor an error during the swap: every GET returns
either the complete old version or the complete new one. The real test
(``tests/test_secret_rotation.py``) proves it with a concurrent reader looping
across N rotations — zero error window.

What local rotation does NOT do (being honest): for third-party credentials
(Slack bot token, GitHub App key, Jira service account) real rotation requires
calling the provider's API to ISSUE the new credential before writing it to
Vault. With no real apps/credentials in this session, the default ``generator``
produces cryptographically strong random material — the mechanism (versioned
write + read-back verification + audit) is identical; plugging in the provider
means implementing one ``generator`` per integration (interface documented
below).

P1: no LLM decisions — schedule + deterministic generators.
P6: a verification failure => clean RotationError; the previous version stays
    intact in Vault (KV v2 preserves history), nothing is truncated.
P8: ONE audit row per rotation (``service_secret_rotated``) with path, old/new
    versions and the key names — NEVER the values.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import secrets as _pysecrets
from typing import Any, Callable

from dse_audit import emit
from dse_secrets import SecretsClient
from dse_secrets.client import VaultUnavailableError

# generator: takes the current dict (may be {} if the path does not exist yet)
# and returns the complete NEW dict. Per-provider implementations (Slack/GitHub/
# Jira) must issue the new credential on the provider's API in here and only
# then return — writing to Vault and auditing stay with rotate_secret().
Generator = Callable[[dict[str, Any]], dict[str, Any]]


class RotationError(RuntimeError):
    """Rotation failed cleanly (P6) — the previous version is still in place."""


def default_generator(current: dict[str, Any]) -> dict[str, Any]:
    """Regenerates strong random material for every existing key of the secret
    (or a ``value`` key if the path is empty). Intended for internal service
    secrets (webhook HMAC, session secrets, internal tokens)."""
    keys = sorted(current.keys()) or ["value"]
    return {k: _pysecrets.token_urlsafe(32) for k in keys}


@dataclasses.dataclass(frozen=True)
class RotationResult:
    path: str
    old_version: int | None
    new_version: int
    rotated_keys: list[str]
    rotated_at: str


def _current_version(client: SecretsClient, path: str) -> int | None:
    """Current version from the KV v2 metadata (None if the path does not exist)."""
    import requests

    url = f"{client.vault_addr}/v1/{client.mount_point}/metadata/{path}"
    try:
        resp = requests.get(url, headers={"X-Vault-Token": client.token}, timeout=client.timeout)
    except requests.RequestException as exc:
        raise VaultUnavailableError(f"Vault unreachable while reading metadata of '{path}': {exc}") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise VaultUnavailableError(f"metadata '{path}' -> HTTP {resp.status_code}: {resp.text}")
    return int(resp.json()["data"]["current_version"])


def rotate_secret(
    path: str,
    *,
    actor: str,
    tenant_id: str = "platform",
    generator: Generator = default_generator,
    client: SecretsClient | None = None,
    conn=None,
) -> RotationResult:
    """Rotates ONE service secret with no downtime for active readers.

    Sequence: read the current value (if any) → ``generator`` produces the new
    one → write (new version, atomic) → **verify by read-back** that the current
    version returns exactly the new material → audit. Any failure raises
    ``RotationError``/``VaultUnavailableError`` — it never leaves the secret in
    an intermediate state (KV v2 is versioned; no intermediate state is even
    possible)."""
    client = client or SecretsClient()

    old_version = _current_version(client, path)
    current: dict[str, Any] = {}
    if old_version is not None:
        current = client.get_secret(path)

    new_data = generator(current)
    if not isinstance(new_data, dict) or not new_data:
        raise RotationError(f"generator of '{path}' returned invalid material (a non-empty dict was expected)")
    if new_data == current:
        raise RotationError(f"generator of '{path}' returned the SAME material — a no-op rotation is refused (P6)")

    client.put_secret(path, new_data)

    # Read-back verification: what a reader sees NOW is the new material.
    readback = client.get_secret(path)
    if readback != new_data:
        raise RotationError(
            f"post-rotation verification of '{path}' failed: read-back does not match the new material"
        )
    new_version = _current_version(client, path)
    if new_version is None or (old_version is not None and new_version <= old_version):
        raise RotationError(
            f"rotation of '{path}' did not advance the KV v2 version (old={old_version}, new={new_version})"
        )

    rotated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    emit(
        actor=actor,
        action="service_secret_rotated",
        tenant_id=tenant_id,
        details={
            "path": path,
            "mount": client.mount_point,
            "old_version": old_version,
            "new_version": new_version,
            "rotated_keys": sorted(new_data.keys()),  # names, NEVER values
            "rotated_at": rotated_at,
        },
        conn=conn,
    )
    return RotationResult(
        path=path,
        old_version=old_version,
        new_version=new_version,
        rotated_keys=sorted(new_data.keys()),
        rotated_at=rotated_at,
    )


def rotate_from_manifest(
    manifest: list[dict[str, Any]],
    *,
    actor: str = "system:secret-rotator",
    client: SecretsClient | None = None,
) -> list[RotationResult | RotationError]:
    """Rotates the list of secrets in the manifest (scheduled job). Each entry:
    ``{"path": "dse/service/<name>", "tenant_id": "platform"}``. One failure does
    NOT abort the rest (each rotation is independent); failures are returned (and
    the caller logs them) — never swallowed."""
    results: list[RotationResult | RotationError] = []
    for entry in manifest:
        try:
            results.append(
                rotate_secret(
                    entry["path"],
                    actor=actor,
                    tenant_id=entry.get("tenant_id", "platform"),
                    client=client,
                )
            )
        except (RotationError, VaultUnavailableError, KeyError) as exc:
            results.append(RotationError(f"{entry!r}: {exc}"))
    return results
