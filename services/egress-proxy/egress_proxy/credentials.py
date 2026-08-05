"""Ephemeral credential injection (WSC-E2-T2).

Model: the sandbox container NEVER receives the real token (not in env, not in
a file, not in a process argument). It sends, through the proxy, a placeholder
header (`X-Dse-Inject-Credential: github`) on an ordinary HTTP request; the
proxy swaps that placeholder for the real token before forwarding the request
outbound — the process inside the sandbox never sees the token value, it only
knows that "something" was injected downstream.

Two `CredentialBroker` implementations:
  - `mint()` tries to mint a REAL GitHub App installation access token (a JWT
    signed with the App private key + exchange via
    `POST /app/installations/{id}/access_tokens`) IF
    `GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY_PATH`/`GITHUB_APP_INSTALLATION_ID`
    are configured via env (production, via Vault/ESO — WS-F).
  - Otherwise (none of these is present in this parallel development session —
    there is no registered GitHub App), it falls back to an opaque fixture
    token, kept only in the proxy process memory. Explicitly documented — see
    the README "what is missing for production".

Revocation SLO: `revoke()` documents and measures the time between the call and
the `revoked_at` marking — the test (`test_credential_injection_and_revocation
.py`) proves it stays under 60s (in fixture mode this is immediate; in real mode
it depends on the GitHub API latency, which typically answers well under 1s for
DELETE /installation/token).
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass

from . import leases_store

REVOCATION_SLO_SECONDS = 60.0


class GitHubScopeError(Exception):
    """Raised when an action outside the token's scope is attempted (e.g.
    opening a PR with a token that only has `contents:write`)."""


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
        """Should never be called by a Phase 1 Coder (P1: no flow decision made
        by an LLM/sandbox) — it exists only to prove, in the adversarial test,
        that EVEN IF something tried, the token scope itself refuses (a second
        enforcement layer, independent of the toolset)."""
        if "pull_requests:write" not in self.allowed_actions:
            raise GitHubScopeError(
                f"credential {self.credential_id} has scope {sorted(self.allowed_actions)} "
                "— pull_requests:write not included; opening a PR is always done by "
                "WS-E's human-triggered PR finalizer, never by the sandbox"
            )

    def force_push(self, *_args, **_kwargs) -> None:
        raise GitHubScopeError(
            f"credential {self.credential_id} has no scope for force-push "
            "(allowed_actions does not include 'contents:force-write')"
        )


class CredentialBroker:
    def __init__(self) -> None:
        self._issued: dict[str, ScopedCredential] = {}
        #: repo -> (token, expires_at). A clone is THREE requests (info/refs plus
        #: two upload-pack POSTs under protocol v2) and each was minting a fresh
        #: 1-hour installation token that nothing ever revoked. Reusing one for
        #: its lifetime turns 3 tokens per clone into 1 and removes two blocking
        #: round-trips from the request path.
        self._token_cache: dict[str, tuple[str, float]] = {}

    @staticmethod
    def enabled() -> bool:
        """`DSE_EGRESS_CREDENTIAL_BROKER_ENABLED` is rendered into the chart's
        ConfigMap and was read by nothing — a kill switch that did not switch
        anything off. It does now."""
        return os.environ.get("DSE_EGRESS_CREDENTIAL_BROKER_ENABLED", "true").lower() not in (
            "0", "false", "no", "off",
        )

    def _mint_real_github_app_token(self, *, repo: str) -> str | None:
        if not self.enabled():
            return None
        cached = self._token_cache.get(repo)
        if cached and cached[1] > time.time() + 300:
            return cached[0]
        app_id = os.environ.get("GITHUB_APP_ID")
        installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID")
        # The PEM by CONTENT is the shape the rest of the platform uses
        # (dse_validation.config.GitHubConfig, and the `dse-poc-secrets` key the
        # chart mounts). Requiring a PATH here meant the broker could never be
        # configured from the same secret as everything else, so it silently
        # fell back to a fixture token forever.
        private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY")
        private_key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
        if not (app_id and installation_id and (private_key or private_key_path)):
            return None

        import jwt  # PyJWT — only imported when actually configured
        import httpx

        if not private_key:
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
            # READ, not write. The relay exists to CLONE; the turn's commits go
            # to the local bare checkpoint, never to GitHub, and the PR is opened
            # by the control plane with its own client. The repo name here comes
            # from a header the sandbox writes, so it chooses which repo in the
            # installation the token covers — bounding the permission is what
            # keeps that choice from mattering. A write token would have let an
            # LLM-driven sandbox push to any repo the App can see.
            json={"repositories": [repo.split("/")[-1]], "permissions": {"contents": "read"}},
            timeout=5.0,
        )
        resp.raise_for_status()
        token = resp.json()["token"]
        # GitHub installation tokens live one hour; cache just under that.
        self._token_cache[repo] = (token, time.time() + 55 * 60)
        return token

    def mint(self, *, work_item_id: str, repo: str, branch: str) -> ScopedCredential:
        real_token = None
        try:
            real_token = self._mint_real_github_app_token(repo=repo)
        except Exception:  # noqa: BLE001 - degrade to the fixture, never fail the task over this
            real_token = None

        fixture = real_token is None
        token = real_token or f"fixture-ghtoken-{uuid.uuid4().hex}"

        cred = ScopedCredential(
            credential_id=uuid.uuid4().hex,
            token=token,
            work_item_id=work_item_id,
            repo=repo,
            branch=branch,
            allowed_actions=frozenset({"contents:read"}),  # never pull_requests:write, never force
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
        """Returns the revocation latency in seconds. Raises if the 60s SLO is
        exceeded (P6: clean, visible failure, never a silent one)."""
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
                f"revocation of {credential_id} took {latency:.1f}s, above the SLO of "
                f"{REVOCATION_SLO_SECONDS}s"
            )
        return latency

    def get(self, credential_id: str) -> ScopedCredential | None:
        return self._issued.get(credential_id)
