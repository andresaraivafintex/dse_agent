# services/egress-proxy (WS-C)

Default-deny proxy + ephemeral credential injection. The only network egress point
that sandbox containers (`services/sandbox-runtime`) can reach — see
`docker-compose.wsc.yml` and the `services/sandbox-runtime` README for the full
network topology.

## What is implemented and working (tested against real Postgres/Docker/sockets)

### WSC-E2-T1 — Default-deny proxy

- `proxy.py`: a real HTTP/HTTPS proxy in pure `asyncio` (stdlib, no
  mitmproxy) — it supports `CONNECT host:port` (opaque tunnel, for HTTPS) and
  plaintext HTTP requests with an absolute URI (`GET http://host/path
  HTTP/1.1`, the classic forward-proxy form).
- `allowlist.py`: `Allowlist.for_work_item(...)` derives the allowlist from the
  `WorkItem` — the repo host (`github.com`/`api.github.com`), the
  model-gateway (WS-D, port 4000) and the package registries
  (`pypi.org`/`files.pythonhosted.org`/`registry.npmjs.org`). Any host
  outside that returns `403` and emits `dse_audit.emit(action="egress_denied",
  details={"host": ...})` — **written to real Postgres** (never mocked; see
  `tests/test_allowlist_and_audit.py::test_disallowed_host_denial_is_audited_in_real_postgres`).
- CONNECT (HTTPS tunnel) is subject to the same allowlist — proven by
  `test_connect_tunnel_enforces_allowlist_too`.

### WSC-E2-T2 — Ephemeral credentials, never inside the sandbox

- `credentials.py::CredentialBroker`: mints a `ScopedCredential` scoped to
  `{"contents:write"}` — never `pull_requests:write`, never force-push.
  `ScopedCredential.create_pull_request()`/`.force_push()` always raise
  `GitHubScopeError`, modeling the real behavior of a GitHub App token
  with restricted permissions.
- Injection: the sandbox container sends a **placeholder** header
  (`X-Dse-Inject-Credential: github`) on an HTTP request through the proxy;
  `proxy.py` swaps that placeholder for the real token (`Authorization: token
  <real>`) before forwarding it outbound. The container never sees the real value.
  Proven by
  `tests/test_credential_injection_and_revocation.py::test_no_token_reaches_sandbox_container_env_fs_or_proc`,
  which runs a real Docker container, makes the call through the proxy, and then
  sweeps `env`, `/tmp` and `/proc/*/environ` of that SAME container proving that
  the real token never appeared there.
- Revocation: `CredentialBroker.revoke()` measures revocation latency and
  raises `TimeoutError` if it exceeds `REVOCATION_SLO_SECONDS = 60.0`
  (P6 — clean, visible failure, never silent). Every mint/revoke is
  persisted in `egress_credential_leases` (migration `0004_wsc.sql`) with
  `issued_at`/`revoked_at`/`revoke_latency_s` — durable, queryable evidence
  of the SLO, on top of `audit_log` (`dse_audit`).

### WSC-E2-T3 — Model-gateway as the only allowlist entry for model calls

- `Allowlist.for_work_item(...)` adds exactly ONE entry of category
  `model_gateway` (the WS-D/LiteLLM host:port) — never
  `api.anthropic.com`/`api.openai.com`/`bedrock-runtime.*`/etc.
  `tests/test_model_gateway_only_allowlist.py` proves that a direct attempt
  at each of those 4 known providers is blocked (403) AND
  audited (a row in `audit_log` with `details->>'host'` = the provider's
  host), and that a call to the real model-gateway host goes through.

## What runs on a local fixture/mock

- **`CredentialBroker` with no real GitHub App registered**: `mint()` tries to
  mint a real installation access token (JWT signed via `PyJWT` +
  `POST /app/installations/{id}/access_tokens`) IF
  `GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY_PATH`/`GITHUB_APP_INSTALLATION_ID`
  are configured via env. No GitHub App is registered in this
  development session — it falls back to an opaque fixture token
  (`fixture-ghtoken-<uuid>`), explicitly flagged in
  `ScopedCredential.fixture = True`. The real path's code (JWT
  signing + token exchange + revocation DELETE) is written and syntactically
  valid against GitHub's public API, but has not been exercised with
  real credentials.
- **"Bare" proxy container in the network isolation test**
  (`services/sandbox-runtime/tests/test_network_isolation.py`): it runs
  `python:3.11-slim` with this package's source bind-mounted, with NO `pip
  install` (no `dse_audit`, no `psycopg2`) — deliberate, as it is the cheapest
  production scenario (no custom image build). In that mode, the egress
  denial still happens (real 403) but the audit falls back to a local stdout log
  (guarded import in `proxy.py`). The proof that the audit REALLY writes to
  Postgres is in this directory's tests (`test_allowlist_and_audit.py`,
  `test_model_gateway_only_allowlist.py`), running `EgressProxy` in-process
  in the venv that has `dse_audit`/`psycopg2` installed.
- **Credential injection is only implemented for the "plaintext HTTP proxy"
  path**, not for opaque `CONNECT`/HTTPS tunnels: intercepting and
  rewriting inside a TLS tunnel would require terminating TLS at the proxy (a
  trusted custom CA installed in the sandbox — which is what mitmproxy does). Design
  decision (documented, not hidden): for the real use case (git push
  to GitHub), production should configure the task's remote as an
  HTTP URL pointing at the proxy itself (`http://egress-proxy:8806/git-relay/
  <work_item_id>`), which the proxy resolves and forwards as HTTPS to
  `github.com` on the outside, injecting the token — with no need for TLS MITM
  in the sandbox. That git-specific relay (`/git-relay/...`) is not
  implemented yet (only the generic placeholder-header injection
  mechanism is); see "what is missing for production".

## What is missing for production

- **Register a real GitHub App** (App ID, private key, installation
  ID) and configure it via Vault/ESO (WS-F) — today it is only
  `GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY_PATH`/`GITHUB_APP_INSTALLATION_ID`
  as env vars (documented, not provisioned in this session).
- **A `/git-relay/<work_item_id>` endpoint** on the proxy: a real reverse proxy
  to `https://github.com/<owner>/<repo>.git` with the GitHub App token
  injected as `Authorization`, letting the sandbox's `git remote`
  point at a local plain-HTTP path on the proxy instead of github.com directly
  — today the placeholder-header injection mechanism exists and is tested,
  but the git-specific HTTP→HTTPS relay has not been implemented yet (the
  local checkpoint path uses a local bare repo instead, as
  permitted by the task statement).
- **A production egress-proxy image with pinned dependencies**
  (`pip install -e services/egress-proxy` into its own image, instead of the
  bind mount on `python:3.11-slim` used in dev) — required for the audit to
  work out of the box in production (today it falls back to the local log
  if `dse_audit`/`psycopg2` are not installed in the image).
- **TLS-interception mitigation for arbitrary HTTPS hosts** (package registries
  such as npm/pypi that may need credentials too) — today
  only the host:port allowlist is enforced on CONNECT tunnels; no
  credential injection happens inside them.

## How to run the tests

```bash
python3.12 -m venv .venv-wsc
source .venv-wsc/bin/activate
pip install -e ../../packages/contracts -e ../../packages/dse_audit -e ../../packages/dse_identity
pip install -e ../sandbox-runtime -e .   # sandbox-runtime is only used by 1 test (docker)
pip install pytest docker

DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse \
  pytest -q services/egress-proxy/tests
```

Requires the foundation's Postgres at `localhost:5432` (for the
audit/revocation tests) and Docker running (for
`test_no_token_reaches_sandbox_container_env_fs_or_proc`).

**Actual result in this session**: `13 passed`, `0 failed`, `0 skipped`.
