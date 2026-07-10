# services/egress-proxy (WS-C)

Proxy default-deny + injeção de credenciais efêmeras. Único ponto de saída
de rede que os containers de sandbox (`services/sandbox-runtime`) conseguem
alcançar — ver `docker-compose.wsc.yml` e o README de
`services/sandbox-runtime` para a topologia de rede completa.

## O que está implementado e funcionando (testado contra Postgres/Docker/sockets reais)

### WSC-E2-T1 — Proxy default-deny

- `proxy.py`: proxy HTTP/HTTPS real em `asyncio` puro (stdlib, sem
  mitmproxy) — suporta `CONNECT host:port` (túnel opaco, para HTTPS) e
  requisições HTTP em texto puro com URI absoluta (`GET http://host/path
  HTTP/1.1`, o formato clássico de forward-proxy).
- `allowlist.py`: `Allowlist.for_work_item(...)` deriva a allowlist do
  `WorkItem` — host do repo (`github.com`/`api.github.com`), o
  model-gateway (WS-D, porta 4000) e os registries de pacote
  (`pypi.org`/`files.pythonhosted.org`/`registry.npmjs.org`). Qualquer host
  fora disso volta `403` e gera `dse_audit.emit(action="egress_denied",
  details={"host": ...})` — **gravado no Postgres real** (nunca mockado; ver
  `tests/test_allowlist_and_audit.py::test_disallowed_host_denial_is_audited_in_real_postgres`).
- CONNECT (túnel HTTPS) também está sujeito à mesma allowlist — provado por
  `test_connect_tunnel_enforces_allowlist_too`.

### WSC-E2-T2 — Credenciais efêmeras, nunca dentro do sandbox

- `credentials.py::CredentialBroker`: minta um `ScopedCredential` com escopo
  `{"contents:write"}` — nunca `pull_requests:write`, nunca force-push.
  `ScopedCredential.create_pull_request()`/`.force_push()` sempre levantam
  `GitHubScopeError`, modelando o comportamento real de um token de GitHub
  App com permissões restritas.
- Injeção: o container do sandbox envia um header **placeholder**
  (`X-Dse-Inject-Credential: github`) numa requisição HTTP através do proxy;
  o `proxy.py` troca esse placeholder pelo token real (`Authorization: token
  <real>`) antes de encaminhar para fora. O container nunca vê o valor real.
  Provado por
  `tests/test_credential_injection_and_revocation.py::test_no_token_reaches_sandbox_container_env_fs_or_proc`,
  que roda um container Docker de verdade, faz a chamada via proxy, e então
  vasculha `env`, `/tmp` e `/proc/*/environ` do MESMO container provando que
  o token real nunca apareceu lá.
- Revogação: `CredentialBroker.revoke()` mede a latência de revogação e
  levanta `TimeoutError` se ultrapassar `REVOCATION_SLO_SECONDS = 60.0`
  (P6 — falha limpa e visível, nunca silenciosa). Cada mint/revoke é
  persistido em `egress_credential_leases` (migração `0004_wsc.sql`) com
  `issued_at`/`revoked_at`/`revoke_latency_s` — evidência durável e
  consultável do SLO, além do `audit_log` (`dse_audit`).

### WSC-E2-T3 — Model-gateway como única allowlist entry para chamadas de modelo

- `Allowlist.for_work_item(...)` só adiciona UMA entry de categoria
  `model_gateway` (o host:porta do WS-D/LiteLLM) — nunca
  `api.anthropic.com`/`api.openai.com`/`bedrock-runtime.*`/etc.
  `tests/test_model_gateway_only_allowlist.py` prova que uma tentativa
  direta a cada um desses 4 providers conhecidos é bloqueada (403) E
  auditada (linha em `audit_log` com `details->>'host'` = o host do
  provider), e que uma chamada ao host do model-gateway de verdade passa.

## O que está com fixture/mock local

- **`CredentialBroker` sem GitHub App real registrado**: `mint()` tenta
  mintar um installation access token real (JWT assinado via `PyJWT` +
  `POST /app/installations/{id}/access_tokens`) SE
  `GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY_PATH`/`GITHUB_APP_INSTALLATION_ID`
  estiverem configurados via env. Nenhum GitHub App está registrado nesta
  sessão de desenvolvimento — cai para um token opaco de fixture
  (`fixture-ghtoken-<uuid>`), sinalizado explicitamente em
  `ScopedCredential.fixture = True`. O código do caminho real (assinatura
  JWT + troca de token + DELETE de revogação) está escrito e é sintaticamente
  válido contra a API pública do GitHub, mas não foi exercitado com
  credenciais de verdade.
- **Container de proxy "pelado" no teste de isolamento de rede**
  (`services/sandbox-runtime/tests/test_network_isolation.py`): roda
  `python:3.11-slim` com o código-fonte deste pacote bind-montado, SEM `pip
  install` (nem `dse_audit`, nem `psycopg2`) — proposital, é o cenário de
  produção mais barato (sem build de imagem custom). Nesse modo, a recusa de
  egress ainda acontece (403 real) mas o audit cai para log local em stdout
  (import guardado em `proxy.py`). A prova de que o audit REALMENTE grava no
  Postgres está nos testes deste diretório (`test_allowlist_and_audit.py`,
  `test_model_gateway_only_allowlist.py`), rodando `EgressProxy` in-process
  no venv que tem `dse_audit`/`psycopg2` instalados.
- **Injeção de credencial só implementada para o path de "proxy HTTP em
  texto plano"**, não para túneis `CONNECT`/HTTPS opacos: interceptar e
  reescrever dentro de um túnel TLS exigiria terminar TLS no proxy (CA
  própria confiável instalada no sandbox — mitmproxy faz isso). Decisão de
  design (documentada, não escondida): para o caso de uso real (git push
  para GitHub), a produção deveria configurar o remote da tarefa como uma
  URL HTTP apontando para o próprio proxy (`http://egress-proxy:8806/git-relay/
  <work_item_id>`), que o proxy resolve e encaminha como HTTPS para
  `github.com` do lado de fora, injetando o token — sem precisar de MITM TLS
  no sandbox. Esse relay específico de git (`/git-relay/...`) não está
  implementado ainda (só o mecanismo genérico de injeção via header
  placeholder está); ver "o que falta para produção".

## O que falta para produção

- **Registrar um GitHub App de verdade** (App ID, chave privada, installation
  ID) e configurar via Vault/ESO (WS-F) — hoje só `GITHUB_APP_ID`/
  `GITHUB_APP_PRIVATE_KEY_PATH`/`GITHUB_APP_INSTALLATION_ID` como env vars
  (documentado, não provisionado nesta sessão).
- **Endpoint `/git-relay/<work_item_id>`** no proxy: um reverse-proxy real
  para `https://github.com/<owner>/<repo>.git` com o token do GitHub App
  injetado como `Authorization`, permitindo que o `git remote` do sandbox
  aponte para um path plain-HTTP local no proxy em vez de github.com direto
  — hoje o mecanismo de injeção via header placeholder existe e é testado,
  mas o relay HTTP→HTTPS específico de git ainda não foi implementado (o
  path de checkpoint local usa bare repo local em vez disso, conforme
  permitido pelo enunciado da tarefa).
- **Imagem de produção do egress-proxy com dependências pinadas**
  (`pip install -e services/egress-proxy` numa imagem própria, em vez do
  bind-mount em `python:3.11-slim` usado em dev) — necessário para o audit
  funcionar out-of-the-box em produção (hoje cai pro fallback de log local
  se `dse_audit`/`psycopg2` não estiverem instalados na imagem).
- **Mitigação de TLS interception para hosts HTTPS arbitrários** (registries
  de pacote como npm/pypi que podem precisar de credenciais também) — hoje
  só a allowlist por host:porta é aplicada em túneis CONNECT; nenhuma
  injeção de credencial acontece dentro deles.

## Como rodar os testes

```bash
python3.12 -m venv .venv-wsc
source .venv-wsc/bin/activate
pip install -e ../../packages/contracts -e ../../packages/dse_audit -e ../../packages/dse_identity
pip install -e ../sandbox-runtime -e .   # sandbox-runtime só é usado por 1 teste (docker)
pip install pytest docker

DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse \
  pytest -q services/egress-proxy/tests
```

Requer Postgres da fundação em `localhost:5432` (para os testes de
audit/revogação) e Docker rodando (para
`test_no_token_reaches_sandbox_container_env_fs_or_proc`).

**Resultado real nesta sessão**: `13 passed`, `0 failed`, `0 skipped`.
