"""WSF-E2-T3b(a) — Rotação AGENDADA de secrets de serviço (ADR-28 completo).

Mecânica de zero-downtime: o backend é Vault KV **v2** — cada rotação é um
``create_or_update`` que grava uma VERSÃO NOVA atomicamente. Um leitor ativo
(adapter lendo o webhook secret, gateway lendo credencial de provider) nunca
observa "path vazio" nem erro durante a troca: cada GET devolve ou a versão
antiga completa ou a nova completa. O teste real
(``tests/test_secret_rotation.py``) prova isso com um leitor concorrente em
loop durante N rotações — zero janela de erro.

O que a rotação local NÃO faz (honesto): para credenciais de terceiros
(Slack bot token, GitHub App key, service account Jira) a rotação de verdade
exige chamar a API do provedor para EMITIR a credencial nova antes de gravar
no Vault. Sem apps/credenciais reais nesta sessão, o ``generator`` default
gera material aleatório criptograficamente forte — o mecanismo (gravação
versionada + verificação de read-back + audit) é idêntico; plugar o provedor
é implementar um ``generator`` por integração (interface documentada abaixo).

P1: nenhuma decisão por LLM — agenda + generators determinísticos.
P6: falha de verificação => RotationError limpo; a versão anterior continua
    íntegra no Vault (KV v2 preserva histórico), nada é truncado.
P8: UMA linha de audit por rotação (``service_secret_rotated``) com path,
    versões antiga/nova e nomes das chaves — NUNCA os valores.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import secrets as _pysecrets
from typing import Any, Callable

from dse_audit import emit
from dse_secrets import SecretsClient
from dse_secrets.client import VaultUnavailableError

# generator: recebe o dict atual (pode ser {} se o path ainda não existe) e
# devolve o dict NOVO completo. Implementações por provedor (Slack/GitHub/
# Jira) devem emitir a credencial nova na API do provedor aqui dentro e só
# então retornar — a gravação no Vault e o audit ficam com rotate_secret().
Generator = Callable[[dict[str, Any]], dict[str, Any]]


class RotationError(RuntimeError):
    """Rotação falhou de forma limpa (P6) — a versão anterior permanece."""


def default_generator(current: dict[str, Any]) -> dict[str, Any]:
    """Regenera material aleatório forte para cada chave existente do secret
    (ou uma chave ``value`` se o path estiver vazio). Serve para secrets de
    serviço internos (HMAC de webhook, session secrets, tokens internos)."""
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
    """Versão corrente no metadata KV v2 (None se o path não existe)."""
    import requests

    url = f"{client.vault_addr}/v1/{client.mount_point}/metadata/{path}"
    try:
        resp = requests.get(url, headers={"X-Vault-Token": client.token}, timeout=client.timeout)
    except requests.RequestException as exc:
        raise VaultUnavailableError(f"Vault inacessível lendo metadata de '{path}': {exc}") from exc
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
    """Rotaciona UM secret de serviço, sem downtime para leitores ativos.

    Sequência: lê o valor atual (se existir) → ``generator`` produz o novo →
    grava (versão nova, atômica) → **verifica por read-back** que a versão
    corrente devolve exatamente o material novo → audita. Qualquer falha
    levanta ``RotationError``/``VaultUnavailableError`` — nunca deixa o
    secret num estado intermediário (KV v2 é versionado; não existe estado
    intermediário possível)."""
    client = client or SecretsClient()

    old_version = _current_version(client, path)
    current: dict[str, Any] = {}
    if old_version is not None:
        current = client.get_secret(path)

    new_data = generator(current)
    if not isinstance(new_data, dict) or not new_data:
        raise RotationError(f"generator de '{path}' devolveu material inválido (dict não-vazio esperado)")
    if new_data == current:
        raise RotationError(f"generator de '{path}' devolveu o MESMO material — rotação sem efeito recusada (P6)")

    client.put_secret(path, new_data)

    # Verificação de read-back: o que um leitor vê AGORA é o material novo.
    readback = client.get_secret(path)
    if readback != new_data:
        raise RotationError(
            f"verificação pós-rotação de '{path}' falhou: read-back não bate com o material novo"
        )
    new_version = _current_version(client, path)
    if new_version is None or (old_version is not None and new_version <= old_version):
        raise RotationError(
            f"rotação de '{path}' não avançou a versão KV v2 (old={old_version}, new={new_version})"
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
            "rotated_keys": sorted(new_data.keys()),  # nomes, NUNCA valores
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
    """Rotaciona a lista de secrets do manifest (job agendado). Cada entrada:
    ``{"path": "dse/service/<nome>", "tenant_id": "platform"}``. Uma falha
    NÃO aborta as demais (cada rotação é independente); falhas são
    retornadas (e o chamador loga) — nunca engolidas."""
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
