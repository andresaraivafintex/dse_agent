"""WSF-E2-T3b(a) — scheduled rotation of service secrets, against the
foundation's REAL Vault (localhost:8200, dev mode). Never mocked (P8).

The central test is the zero-downtime one: a concurrent reader in a tight loop
across N rotations never sees an error nor an intermediate state — every GET
returns a complete version (old or new), which is exactly the guarantee intake
needs (the webhook secret being read by the adapter while it is swapped).
"""
from __future__ import annotations

import threading
import uuid

import psycopg2
import pytest
from dse_platform import RotationError, rotate_from_manifest, rotate_secret
from dse_platform.jobs_scheduler import run_rotation_once
from dse_secrets import SecretsClient
from dse_secrets.client import VaultUnavailableError

AUDIT_DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


@pytest.fixture(scope="module")
def client():
    try:
        c = SecretsClient()
        c.put_secret("dse/test/rotation-smoke", {"ok": "1"})
    except VaultUnavailableError as exc:
        pytest.skip(f"foundation Vault unavailable: {exc}")
    return c


@pytest.fixture()
def path():
    return f"dse/test/rotation-{uuid.uuid4().hex[:8]}"


def _audit_rows(action: str, path: str) -> list[dict]:
    conn = psycopg2.connect(AUDIT_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT actor, details FROM audit_log WHERE action = %s AND details->>'path' = %s ORDER BY ts",
                (action, path),
            )
            return [{"actor": a, "details": d} for a, d in cur.fetchall()]
    finally:
        conn.close()


def test_rotate_creates_new_version_and_audit_row(client, path):
    client.put_secret(path, {"signing_secret": "old-material"})

    result = rotate_secret(path, actor="system:secret-rotator", client=client)

    assert result.old_version == 1
    assert result.new_version == 2
    assert result.rotated_keys == ["signing_secret"]
    # the material really changed and is what a reader sees now
    live = client.get_secret(path)
    assert live["signing_secret"] != "old-material"

    rows = _audit_rows("service_secret_rotated", path)
    assert len(rows) == 1
    assert rows[0]["actor"] == "system:secret-rotator"
    assert rows[0]["details"]["old_version"] == 1
    assert rows[0]["details"]["new_version"] == 2


def test_audit_row_never_contains_secret_material(client, path):
    client.put_secret(path, {"token": "super-sensitive-old"})
    rotate_secret(path, actor="system:secret-rotator", client=client)
    new_value = client.get_secret(path)["token"]

    rows = _audit_rows("service_secret_rotated", path)
    serialized = str(rows)
    assert "super-sensitive-old" not in serialized
    assert new_value not in serialized
    # only the key NAMES show up
    assert rows[0]["details"]["rotated_keys"] == ["token"]


def test_rotation_without_downtime_for_active_reader(client, path):
    """Acceptance proof: rotate a secret consumed by an active reader with zero
    error window. Reader in a tight loop on a thread; 5 rotations on the main
    flow; every read returns a complete, known state."""
    client.put_secret(path, {"webhook_secret": "v0"})

    stop = threading.Event()
    errors: list[Exception] = []
    seen: list[str] = []
    # separate client (its own connection) — a genuinely independent reader
    reader = SecretsClient()

    def read_loop():
        while not stop.is_set():
            try:
                value = reader.get_secret(path)
                seen.append(value["webhook_secret"])
            except Exception as exc:  # noqa: BLE001 — the point is to catch ANY window
                errors.append(exc)

    t = threading.Thread(target=read_loop, daemon=True)
    t.start()

    valid_values = {"v0"}
    try:
        for _ in range(5):
            result = rotate_secret(path, actor="system:secret-rotator", client=client)
            valid_values.add(client.get_secret(path)["webhook_secret"])
            assert result.new_version is not None
    finally:
        stop.set()
        t.join(timeout=10)

    assert not errors, f"an active reader saw an error during rotation (downtime window!): {errors[:3]}"
    assert len(seen) > 0, "the reader never read anything — invalid test"
    unknown = set(seen) - valid_values
    assert not unknown, f"the reader saw material matching no known version: {unknown}"
    # and all 5 rotations landed in the ledger
    assert len(_audit_rows("service_secret_rotated", path)) == 5


def test_generator_returning_same_material_is_refused(client, path):
    client.put_secret(path, {"k": "same"})
    with pytest.raises(RotationError, match="SAME material"):
        rotate_secret(path, actor="system:secret-rotator", client=client, generator=lambda cur: dict(cur))
    # the current version stayed intact (P6: clean failure)
    assert client.get_secret(path) == {"k": "same"}


def test_generator_returning_empty_is_refused(client, path):
    client.put_secret(path, {"k": "x"})
    with pytest.raises(RotationError, match="invalid material"):
        rotate_secret(path, actor="system:secret-rotator", client=client, generator=lambda cur: {})


def test_rotate_from_manifest_isolates_failures(client, path):
    """One broken entry must not stop the others (the scheduled run is unattended)."""
    client.put_secret(path, {"a": "1"})
    results = rotate_from_manifest(
        [
            {"path": path},
            {"no_path_key": "broken"},
        ],
        client=client,
    )
    assert len(results) == 2
    assert not isinstance(results[0], RotationError)
    assert isinstance(results[1], RotationError)


def test_scheduler_run_rotation_once_uses_manifest_env(client, path, monkeypatch):
    """The scheduled entrypoint (compose platform-jobs / CronJob --once) really
    runs the manifest from env against the real Vault."""
    client.put_secret(path, {"session_secret": "before"})
    monkeypatch.setenv("DSE_ROTATION_MANIFEST", f'[{{"path": "{path}", "tenant_id": "platform"}}]')

    failures = run_rotation_once()

    assert failures == 0
    assert client.get_secret(path)["session_secret"] != "before"
    assert len(_audit_rows("service_secret_rotated", path)) == 1
