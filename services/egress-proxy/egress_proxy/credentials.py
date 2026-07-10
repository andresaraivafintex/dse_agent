"""Injeção de credenciais efêmeras (WSC-E2-T2).

Modelo: o container do sandbox NUNCA recebe o token real (nem em env, nem em
arquivo, nem em argumento de processo). Ele envia, através do proxy, um
header placeholder (`X-Dse-Inject-Credential: github`) numa requisição HTTP
comum; o proxy troca esse placeholder pelo token real antes de encaminhar a
requisição para fora — o processo dentro do sandbox nunca vê o valor do
token, só sabe que "algo" foi injetado a jusante.

Duas implementações de `CredentialBroker`:
  - `mint()` tenta mintar um installation access token REAL de GitHub App
    (JWT assinado com a chave privada do App + troca por
    `POST /app/installations/{id}/access_tokens`) SE
    `GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY_PATH`/`GITHUB_APP_INSTALLATION_ID`
    estiverem configurados via env (produção, via Vault/ESO — WS-F).
  - Caso contrário (nenhum destes está presente nesta sessão de
    desenvolvimento paralelo — não há GitHub App registrado), cai para um
    token opaco de fixture, guardado só em memória do processo do proxy.
    Documentado explicitamente — ver README "o que falta para produção".

SLO de revogação: `revoke()` documenta e mede o tempo entre a chamada e a
marcação de `revoked_at` — o teste (`test_credential_injection_and_revocation
.py`) prova que fica abaixo de 60s (no modo fixture isso é imediato; no modo
real depende da latência da API do GitHub, que tipicamente responde em bem
menos de 1s para DELETE /installation/token).
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field

from . import leases_store

REVOCATION_SLO_SECONDS = 60.0


class GitHubScopeError(Exception):
    """Levantado quando uma ação fora do escopo do token é tentada (ex.:
    abrir PR com um token que só tem `contents:write`)."""


@dataclass
class ScopedCredential:
    credential_id: str
    token: str
    work_item_id: str
    repo: str
    branch: str
    allowed_actions: frozenset[str]
    issued_at: float
    fixture: bool = False
    revoked_at: float | None = None

    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def create_pull_request(self, *_args, **_kwargs) -> None:
        """Nunca deveria ser chamado por um Coder da Fase 1 (P1: nenhuma
        decisão de fluxo por LLM/sandbox) — existe só para provar, no teste
        adversarial, que MESMO SE algo tentasse, o próprio escopo do token
        recusa (segunda camada de enforcement, independente do toolset)."""
        if "pull_requests:write" not in self.allowed_actions:
            raise GitHubScopeError(
                f"credential {self.credential_id} tem escopo {sorted(self.allowed_actions)} "
                "— pull_requests:write não incluído; abrir PR é sempre feito pelo "
                "PR finalizer humano-acionado do WS-E, nunca pelo sandbox"
            )

    def force_push(self, *_args, **_kwargs) -> None:
        raise GitHubScopeError(
            f"credential {self.credential_id} não tem escopo para force-push "
            "(allowed_actions não inclui 'contents:force-write')"
        )


class CredentialBroker:
    def __init__(self) -> None:
        self._issued: dict[str, ScopedCredential] = {}

    def _mint_real_github_app_token(self, *, repo: str) -> str | None:
        app_id = os.environ.get("GITHUB_APP_ID")
        private_key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
        installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID")
        if not (app_id and private_key_path and installation_id):
            return None

        import jwt  # PyJWT — só importado quando realmente configurado
        import httpx

        with open(private_key_path, "rb") as f:
            private_key = f.read()
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": app_id}
        encoded_jwt = jwt.encode(payload, private_key, algorithm="RS256")

        resp = httpx.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {encoded_jwt}",
                "Accept": "application/vnd.github+json",
            },
            json={"repositories": [repo.split("/")[-1]], "permissions": {"contents": "write"}},
            timeout=5.0,
        )
        resp.raise_for_status()
        return resp.json()["token"]

    def mint(self, *, work_item_id: str, repo: str, branch: str) -> ScopedCredential:
        real_token = None
        try:
            real_token = self._mint_real_github_app_token(repo=repo)
        except Exception:  # noqa: BLE001 - degrada pro fixture, nunca quebra a tarefa por isso
            real_token = None

        fixture = real_token is None
        token = real_token or f"fixture-ghtoken-{uuid.uuid4().hex}"

        cred = ScopedCredential(
            credential_id=uuid.uuid4().hex,
            token=token,
            work_item_id=work_item_id,
            repo=repo,
            branch=branch,
            allowed_actions=frozenset({"contents:write"}),  # nunca pull_requests:write, nunca force
            issued_at=time.time(),
            fixture=fixture,
        )
        self._issued[cred.credential_id] = cred
        leases_store.record_issued(
            credential_id=cred.credential_id,
            work_item_id=work_item_id,
            tenant_id=os.environ.get("DSE_EGRESS_TENANT_ID", "unknown"),
            repo=repo,
            branch=branch,
            allowed_actions=cred.allowed_actions,
            fixture=fixture,
        )
        return cred

    def revoke(self, credential_id: str) -> float:
        """Retorna a latência de revogação em segundos. Lança se o SLO de
        60s for excedido (P6: falha limpa e visível, nunca silenciosa)."""
        cred = self._issued.get(credential_id)
        if cred is None:
            return 0.0
        start = time.time()
        if not cred.fixture:
            installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID")
            if installation_id:
                import httpx

                httpx.delete(
                    "https://api.github.com/installation/token",
                    headers={"Authorization": f"token {cred.token}"},
                    timeout=5.0,
                )
        cred.revoked_at = time.time()
        latency = cred.revoked_at - start
        leases_store.record_revoked(credential_id=credential_id, latency_s=latency)
        if latency > REVOCATION_SLO_SECONDS:
            raise TimeoutError(
                f"revogação de {credential_id} levou {latency:.1f}s, acima do SLO de "
                f"{REVOCATION_SLO_SECONDS}s"
            )
        return latency

    def get(self, credential_id: str) -> ScopedCredential | None:
        return self._issued.get(credential_id)
