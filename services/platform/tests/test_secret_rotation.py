"""WSF-E2-T3b(a) — rotação agendada de secrets de serviço, contra o Vault
REAL da fundação (localhost:8200, dev mode). Nunca mockado (P8).

O teste central é o de zero-downtime: um leitor concorrente em loop apertado
durante N rotações nunca vê erro nem estado intermediário — cada GET devolve
uma versão completa (antiga ou nova), que é exatamente a garantia que o
intake precisa (webhook secret sendo lido pelo adapter durante a troca).
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
        pytest.skip(f"Vault da fundação indisponível: {exc}")
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
    # o material realmente mudou e é o que um leitor vê agora
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
    # só os NOMES das chaves aparecem
    assert rows[0]["details"]["rotated_keys"] == ["token"]


def test_rotation_without_downtime_for_active_reader(client, path):
    """Prova do aceite: rotacionar um secret consumido por um leitor ativo,
    zero janela de erro. Leitor em loop apertado numa thread; 5 rotações no
    fluxo principal; toda leitura devolve um estado completo e conhecido."""
    client.put_secret(path, {"webhook_secret": "v0"})

    stop = threading.Event()
    errors: list[Exception] = []
    seen: list[str] = []
    # cliente separado (conexão própria) — leitor independente de verdade
    reader = SecretsClient()

    def read_loop():
        while not stop.is_set():
            try:
                value = reader.get_secret(path)
                seen.append(value["webhook_secret"])
            except Exception as exc:  # noqa: BLE001 — o ponto é capturar QUALQUER janela
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

    assert not errors, f"leitor ativo viu erro durante rotação (janela de downtime!): {errors[:3]}"
    assert len(seen) > 0, "leitor não chegou a ler nada — teste inválido"
    unknown = set(seen) - valid_values
    assert not unknown, f"leitor viu material que não é nenhuma versão conhecida: {unknown}"
    # e as 5 rotações ficaram no ledger
    assert len(_audit_rows("service_secret_rotated", path)) == 5


def test_generator_returning_same_material_is_refused(client, path):
    client.put_secret(path, {"k": "same"})
    with pytest.raises(RotationError, match="MESMO material"):
        rotate_secret(path, actor="system:secret-rotator", client=client, generator=lambda cur: dict(cur))
    # a versão corrente permaneceu intacta (P6: falha limpa)
    assert client.get_secret(path) == {"k": "same"}


def test_generator_returning_empty_is_refused(client, path):
    client.put_secret(path, {"k": "x"})
    with pytest.raises(RotationError, match="inválido"):
        rotate_secret(path, actor="system:secret-rotator", client=client, generator=lambda cur: {})


def test_rotate_from_manifest_isolates_failures(client, path):
    """Uma entrada quebrada não impede as demais (agendado roda sem babá)."""
    client.put_secret(path, {"a": "1"})
    results = rotate_from_manifest(
        [
            {"path": path},
            {"no_path_key": "quebrada"},
        ],
        client=client,
    )
    assert len(results) == 2
    assert not isinstance(results[0], RotationError)
    assert isinstance(results[1], RotationError)


def test_scheduler_run_rotation_once_uses_manifest_env(client, path, monkeypatch):
    """O entrypoint agendado (compose platform-jobs / CronJob --once) roda o
    manifest do env de verdade contra o Vault real."""
    client.put_secret(path, {"session_secret": "before"})
    monkeypatch.setenv("DSE_ROTATION_MANIFEST", f'[{{"path": "{path}", "tenant_id": "platform"}}]')

    failures = run_rotation_once()

    assert failures == 0
    assert client.get_secret(path)["session_secret"] != "before"
    assert len(_audit_rows("service_secret_rotated", path)) == 1
