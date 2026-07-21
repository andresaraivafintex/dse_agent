# services/platform — WS-F (Segurança, compliance, plataforma e operações)

Implementação Fase 1 dos P0 do WS-F. Lê `../../CONVENTIONS.md` primeiro se
ainda não leu — este README assume o vocabulário/contratos de lá.

> **A Fase 2 e a Fase 3 estão em seções no final deste README**
> ("## Fase 2 — o que foi adicionado", "## Fase 3 — o que foi adicionado").
> A Fase 1 abaixo permanece válida e intacta. Resultado atual da suíte
> completa do WS-F (Fases 1+2+3): `121 passed, 2 skipped` (os 2 skips são
> testes adversariais do egress-proxy que exigem sandbox real do WS-C —
> herança da Fase 1).

## O que está implementado e funcionando (contra infra real)

| Item | Onde | Prova |
|---|---|---|
| **WSF-E1-T2 — reconstrução por auditoria** (exit criterion da Fase 1) | `packages/dse_audit/dse_audit/queries.py` (`reconstruct_work_item_history`, `export_audit_range`, `export_audit_range_csv`) | `packages/dse_audit/tests/test_queries.py` — grava a sequência completa `admitted → clarified → plan → implementing → l1_passed → pr_opened → review_approved → merged` no Postgres real e prova que um único `SELECT ... ORDER BY ts` reproduz a ordem exata |
| **WSF-E2-T3a — secrets backend** | `dse_secrets/client.py` (`SecretsClient`, `get_secret`, `put_secret`, `delete_secret`) | `tests/test_dse_secrets_client.py` — roda contra o Vault dev real (`localhost:8200`, `dse_dev_root`), roundtrip put→get, versionamento, delete, e erro claro se token ausente |
| **WSF-E2-T3a — scanner de secrets em texto plano** | `../../scripts/scan_for_plaintext_secrets.py` | `tests/test_scan_for_plaintext_secrets.py` (8 casos: token Slack, chave AWS, PEM, placeholder de dev conhecido, `.env.example` ignorado, referência `os.environ/` não é falso-positivo, diretório gitignored ignorado) + rodado de verdade contra o monorepo (ver seção "Achado real" abaixo) |
| **`tenant_config` — budgets/fairness/kill-switch** | `migrations/0007_wsf.sql` + `dse_platform/tenant_config.py` (`get_tenant_config`, `upsert_tenant_config`, `set_kill_switch`) | `tests/test_tenant_config.py` — upsert idempotente, kill-switch grava 2 linhas de audit (`kill_switch_enabled`/`kill_switch_disabled`) com `actor`/`reason`, contra Postgres real |
| **WSF-E0 — CI de plataforma** | `../../.github/workflows/ci.yml` | Job `lint` (py_compile + ruff best-effort + scanner de secrets) e job `test` (sobe a infra real via `docker compose`, `scripts/migrate.py`, `pip install -e` em todo `packages/*`/`services/*` com `pyproject.toml`, `pytest -q packages services`) — YAML validado com `yaml.safe_load` |
| **WSF-E0 — contracts changelog** | `../../CONTRACTS-CHANGELOG.md` | Versões atuais (`dse_contracts`/`dse_audit`/`dse_identity` 0.1.0), regra de aprovação do arquiteto-chefe para mudanças breaking, e a entrada da extensão aditiva `dse_audit.queries` feita nesta sessão |
| **WSF-E5-T1/T2 — Helm chart (topologia A)** | `../../infra/helm/dse/` | `helm lint infra/helm/dse` → 0 falhas; `helm template` → 33 documentos válidos (parseados com `yaml.safe_load_all`), testado com 4 combinações de flags (`secrets.externalSecrets.enabled`, `vault.externallyManaged`, `postgres.persistence.enabled`, `ingress.enabled`) — **`helm` CLI real instalado e usado** (não simulado) |
| **WSF-E5-T2 — runbook de upgrade** | `../../infra/RUNBOOK-UPGRADE.md` | Referencia (não duplica) `services/orchestrator/RUNBOOK.md` (WS-B) para Worker Versioning/drain-and-cutover |
| **WSF-E5-T2 — OSS BOM** | `../../infra/OSS-BOM.md` | Licenças de Postgres/Temporal/Redis/Vault/OTel + libs Python principais, incluindo alerta honesto sobre Vault (BUSL) e Redis (RSAL/SSPL) |
| **WSF-E7-T1 — OTel collector** | `../../docker-compose.wsf.yml` + `../../infra/otel-collector-config.yaml` + `infra/helm/dse/templates/otel-collector.yaml` | Config validada (`docker compose config --quiet` → exit 0); recebe OTLP grpc/http, usa os atributos `dse_contracts.constants.OTEL_ATTR_*` como contrato de quem emite |
| **WSF-E7-T1 — regras de alerting** | `../../infra/ALERTING-RULES.md` | 3 regras (exaustão de budget, egress denies não resolvidos, aproximação do limite de history do Temporal) especificadas contra os atributos OTel reais |
| **WSF-E2 — testes adversariais do egress proxy (papel de sign-off)** | `tests/test_egress_proxy_adversarial.py` | 14 casos escritos contra a interface assumida (forward-proxy HTTP em `:8806`) — **todos SKIPADOS** nesta sessão porque `services/egress-proxy` (WS-C) ainda não está no ar (`localhost:8806` recusa conexão) |

## Achado real do scanner de secrets (não escondido)

Rodar `python3 scripts/scan_for_plaintext_secrets.py --root .` a partir da
raiz do monorepo encontra **1 ocorrência real**:

```
services/model-gateway/litellm_config.yaml:53  [generic_api_key_assignment]  api_key: "sk-eco-local-dev-not-a-real-key
```

É um valor claramente fake (`not-a-real-key`, tier local `eco/echo-model`
sem custo, não é um secret de produção) do WS-D — fora do diretório do
WS-F, então não editei o arquivo (regra de convivência: só edito meu
próprio diretório). O scanner está funcionando corretamente (o padrão
"chave/senha hardcoded" bate mesmo em valores fake, por design — a decisão
de "é aceitável" precisa ser humana, não um heurístico de "parece fake").
Sinalizei via task separada para o WS-D trocar por uma referência
`os.environ/DSE_ECHO_API_KEY` (consistente com o resto do arquivo).

O scanner usa `git ls-files` (rastreados) UNIÃO `git ls-files --others
--exclude-standard` (não rastreados mas não ignorados) como o universo de
arquivos "versionáveis" — isso evita falso-positivo em `.venv-*/` (que têm
dezenas de `api_key: os.environ/...` de exemplo dentro do próprio pacote
`litellm` instalado) sem depender de nada estar de fato commitado ainda
(nenhum workstream commita nesta fase — `git status --porcelain` mostra só
arquivos `??` untracked).

## O que está com fixture/mock local

- **Vault**: usa o `vault` em modo **dev** da fundação (`localhost:8200`,
  root token `dse_dev_root`) — dev mode não persiste em disco de forma
  segura e usa uma única unseal key. `dse_secrets.client.SecretsClient`
  funciona identicamente contra um Vault de produção (HTTP API real via
  `hvac`); só o *servidor* é dev-mode nesta sessão, não o cliente.
- **egress-proxy**: `services/egress-proxy` (WS-C) não estava no ar quando
  esta suíte foi escrita/rodada — os 14 testes adversariais em
  `tests/test_egress_proxy_adversarial.py` skipam com razão clara. A
  interface exata (forward-proxy HTTP puro vs. API REST) é uma SUPOSIÇÃO
  documentada no topo do arquivo, não confirmada com o WS-C.
- **Backend de alerting**: `infra/ALERTING-RULES.md` é documentação de
  regras, não alertas ativos — nenhum Alertmanager/Datadog/Grafana está
  conectado ao `otel-collector` (que hoje só faz `debug` export/stdout).
- **Budget real (`dse.cost_usd`)**: nenhum serviço está gravando custo real
  de provider ainda (sem conta AWS/Bedrock provisionada — WS-D usa o tier
  `eco/echo-model` local, custo zero) — a regra de alerting 1 está
  especificada mas não tem dado real para disparar ainda.

## O que precisa de credencial/infra real para produção

- **Vault de produção**: trocar `vault.devMode: true` (chart) por
  `vault.externallyManaged: true` apontando para o Vault (ou OpenBao, ver
  `infra/OSS-BOM.md`) HA real do cliente. `VAULT_TOKEN` de produção nunca
  deve ser o root token — criar uma policy dedicada por serviço.
- **External Secrets Operator**: `secrets.externalSecrets.enabled: true`
  requer o ESO instalado no cluster do cliente (CRD `ExternalSecret`) — o
  chart gera o manifest, mas não instala o operator (fora do escopo deste
  chart de aplicação).
- **Backend de alerting real**: escolha do cliente (Grafana/Datadog/
  Alertmanager) + exporter correspondente no `otel-collector` — ver seção
  final de `infra/ALERTING-RULES.md`.
- **Cluster K8s real**: nenhum cluster de cliente disponível nesta sessão —
  os Helm charts foram validados com `helm lint`/`helm template` (sintaxe e
  renderização corretas) mas **nunca aplicados com `helm install` contra um
  cluster de verdade**. `helm CLI` foi instalado no ambiente (`brew install
  helm`, versão 4.2.3) especificamente para esta validação.
- **Egress-proxy real do WS-C**: reexecutar `tests/test_egress_proxy_adversarial.py`
  assim que `services/egress-proxy` subir em `:8806` — e revisar as
  suposições de interface documentadas no topo do arquivo contra o que o
  WS-C realmente publicar (podem não bater, especialmente os testes de
  reuso de token/credential-broker, que dependem de um contrato de API
  ainda não documentado).

## `dse_secrets` — contrato de consumo estável (cross-workstream)

WS-A, WS-C e WS-D devem importar isto para ler webhook secrets/tokens de
serviço/credenciais de provider em vez de env vars em texto plano:

```python
from dse_secrets import get_secret, put_secret, SecretsClient

# uso simples (constrói um cliente a partir de VAULT_ADDR/VAULT_TOKEN)
creds = get_secret("dse/slack/webhook")        # -> {"signing_secret": "..."}
put_secret("dse/github-app/private-key", {"pem": "..."})

# uso intensivo (reutiliza conexão)
client = SecretsClient()                        # ou SecretsClient(vault_addr=..., token=...)
client.get_secret("dse/model-gateway/bedrock")
client.delete_secret("dse/rotated-key")          # soft-delete, KV v2 preserva histórico
```

Configuração (env var — nunca hardcode):

- `VAULT_ADDR` (default `http://localhost:8200`)
- `VAULT_TOKEN` (produção) ou `VAULT_DEV_ROOT_TOKEN` (dev local apenas)
- `VAULT_KV_MOUNT` (default `secret` — o mount KV v2 que o Vault dev sobe
  por padrão; produção deve usar um mount dedicado)

Levanta `dse_secrets.VaultUnavailableError` (nunca deixa uma exceção de
`hvac`/`requests` vazar sem contexto) em qualquer falha — path ausente,
token inválido, Vault fora do ar.

## `dse_platform.tenant_config` — budgets/fairness/kill-switch

```python
from dse_platform import get_tenant_config, upsert_tenant_config, set_kill_switch

cfg = upsert_tenant_config("acme", monthly_budget_usd=500)
set_kill_switch("acme", enabled=True, reason="budget exceeded", actor="system:budget-monitor")
```

Toda mudança de kill-switch grava uma linha de audit (`kill_switch_enabled`/
`kill_switch_disabled`) via `dse_audit.emit` na mesma transação — nunca
silenciosa (P8). Ativar o kill-switch sem `reason` levanta `ValueError`.

## `dse_audit.queries` — extensão aditiva do WS-F sobre o pacote da fundação

Nota de processo (ver `../../CONTRACTS-CHANGELOG.md` para o texto completo):
as instruções gerais de convivência deste programa dizem para não editar
`packages/dse_audit` (é listado como "fundação compartilhada" no boilerplate
comum a todos os workstreams). Mas `CONVENTIONS.md` — o documento que este
mesmo processo mandou ler primeiro como fonte de verdade — é explícito:
*"packages/dse_audit/ | Fundação (mínimo) → **WS-F estende**"* e a tarefa
WSF-E1-T2 pede literalmente `packages/dse_audit/dse_audit/queries.py`. Segui
`CONVENTIONS.md` (mais específico e é o documento de step-0 mandatório),
e limitei a mudança ao mínimo aditivo possível para reduzir risco de colisão
com os outros 5 workstreams editando em paralelo:

- **Não toquei** `dse_audit/client.py` (o único caminho de escrita,
  `emit`, continua exatamente como estava).
- **Só adicionei** um arquivo novo (`dse_audit/queries.py`) e um teste novo
  (`tests/test_queries.py`).
- **Em `__init__.py`** só acrescentei os 3 símbolos novos aos já existentes
  — `emit` e `get_connection` continuam exportados, nada foi removido ou
  renomeado (a regra de "aditivo sempre permitido" de
  `CONTRACTS-CHANGELOG.md`).

Se isso for revertido na consolidação (porque o arquiteto-chefe decidir que
a leitura estrita da lista de diretórios proibidos deveria ter prevalecido),
o código está isolado o suficiente para ser removido sem tocar em mais nada.

## Como rodar os testes

```bash
# 1. venv isolado do WS-F (não reusar .venv/ da fundação)
python3.12 -m venv /Users/saraiva/Documents/DSE/fase1/.venv-wsf
source /Users/saraiva/Documents/DSE/fase1/.venv-wsf/bin/activate

# 2. instala os pacotes da fundação + este serviço
pip install -e packages/contracts -e packages/dse_audit -e packages/dse_identity
pip install -e services/platform
pip install pytest

# 3. env vars (infra já está no ar — ver CONVENTIONS.md)
export DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse
export DSE_AUDIT_DATABASE_URL=postgresql://dse_app:dse_app_dev_only@localhost:5432/dse
export DSE_PLATFORM_DATABASE_URL=postgresql://dse_app:dse_app_dev_only@localhost:5432/dse
export VAULT_ADDR=http://localhost:8200
export VAULT_DEV_ROOT_TOKEN=dse_dev_root

# 4. aplica a migração reservada do WS-F (idempotente)
python3 scripts/migrate.py

# 5. roda os testes
pytest -q packages/dse_audit services/platform

# 6. valida o Helm chart (requer `helm` — instalado via `brew install helm`
#    nesta sessão; se ausente, ao menos rode a validação manual de YAML)
helm lint infra/helm/dse
helm template dse-test infra/helm/dse | python3 -c "import yaml,sys; list(yaml.safe_load_all(sys.stdin))" && echo "YAML OK"

# 7. scanner de secrets
python3 scripts/scan_for_plaintext_secrets.py --root .
```

## Resultado real (última execução nesta sessão)

```
$ pytest -q packages/dse_audit services/platform
.............ssssssssssssss.............                                 [100%]
26 passed, 14 skipped in 1.81s
```

Os 14 skipped são os testes adversariais do egress-proxy (`services/egress-proxy`
do WS-C não estava respondendo em `localhost:8806` no momento da execução —
não é uma falha, é o skip documentado esperado). **Zero testes falhando.**

```
$ helm lint infra/helm/dse
==> Linting infra/helm/dse
[INFO] Chart.yaml: icon is recommended
1 chart(s) linted, 0 chart(s) failed
```

```
$ python3 scripts/scan_for_plaintext_secrets.py --root .
[scan_for_plaintext_secrets] FALHOU — 1 possível(is) secret(s) em texto plano:
  services/model-gateway/litellm_config.yaml:53 [...]
```
(exit code 1 — comportamento correto e esperado; ver "Achado real" acima).

## Estrutura

```
services/platform/
  dse_secrets/          cliente do Vault (WSF-E2-T3a)
  dse_platform/          tenant_config (budgets/fairness/kill-switch)
  tests/
    test_dse_secrets_client.py
    test_scan_for_plaintext_secrets.py
    test_tenant_config.py
    test_egress_proxy_adversarial.py   (WSF-E2 — sign-off, skip se WS-C down)
  pyproject.toml

packages/dse_audit/dse_audit/queries.py   (extensão aditiva WSF-E1-T2)
packages/dse_audit/tests/test_queries.py  (exercício de reconstrução)

migrations/0007_wsf.sql   (tenant_config)

scripts/scan_for_plaintext_secrets.py

infra/
  helm/dse/              chart Helm (topologia A)
  otel-collector-config.yaml
  RUNBOOK-UPGRADE.md
  OSS-BOM.md
  ALERTING-RULES.md

docker-compose.wsf.yml   (otel-collector)

.github/workflows/ci.yml
CONTRACTS-CHANGELOG.md
```

---

## Fase 2 — o que foi adicionado

A Fase 2 do WS-F ("access bundles, ADR-22/SSO, isolamento multi-tenant, queue
board") é **aditiva** sobre a Fase 1. Nada da Fase 1 foi removido/renomeado.
Migração reservada: `migrations/0013_wsf2.sql`. Porta nova: **8890** (queue board).

### Mapa de entrega (Fase 2)

| Tarefa | Onde | Prova |
|---|---|---|
| **WSF-E3-T2 — Access bundles por tenant/canal** | `dse_platform/access_bundles.py` + `migrations/0013_wsf2.sql` (`dse_access_bundle`) | `tests/test_access_bundles.py` — CRUD, resolução canal-sobre-default, deny-by-default (sem bundle nega repo/mode), `blocked_actions` (ex. `direct_merge_to_protected_branch`), **cascata de approvers vazia BLOQUEIA** (`NoApproverError`, P3), offboardado removido da cascata |
| **WSF-E3-T3 — ADR-22 + SSO/OIDC do console** | `infra/ADR-22-identity.md` (design), `dse_platform/sso.py` (`OIDCVerifier`/`login`/`offboard`/`provision_console_user`), `dse_platform/dev_idp.py` (IdP OIDC de dev), `dse_platform/steering_resolution.py`, login em `queue_board/app.py` | `tests/test_sso.py` — verificação RSA real (assinatura/iss/aud/exp), account matching por `sub` estável, login JIT, **offboarding nega login E remove de approver/steering**, contractor expira |
| **WSF-E4-T3 — Suíte de isolamento multi-tenant (NFR-03)** | `dse_platform/tenant_isolation.py` | `tests/test_tenant_isolation.py` — camada a camada (filas/fairness keys, artifacts/prefixos, skills, retrieval, audit, tokens) com **tentativas ATIVAS cross-tenant** que falham (`CrossTenantViolation`) e são auditadas (`cross_tenant_access_denied`) |
| **WSF-E6-T1 — API do queue board** | `dse_platform/queue_board/api.py` | `tests/test_queue_board.py` — projeção §9.3 (todos os estados), `to_public_status` reusado, budgets + custo agregado, `active_work_items`, quarentena, trilha de audit |
| **WSF-E6-T2 — Controles de operador → signals Temporal** | `dse_platform/queue_board/operator.py` + `signals.py` + `dse_platform/kill_switches.py` | `tests/test_queue_board.py` + `tests/test_kill_switches.py` — pause/resume/cancel/retry/reassign model+runtime/force_clarification/escalate/quarantine + **kill switches nos 4 escopos** (global/tenant/canal/task); cada ação auditada com identidade do operador; intenção auditada mesmo se o signal falhar |
| **WSF-E6-T3 — UI mínima (server-rendered, 8890)** | `dse_platform/queue_board/app.py` + `asgi.py` + `services/platform/Dockerfile` + fragment `docker-compose.wsf.yml` | `tests/test_queue_board_app.py` — SSO gate (401 sem sessão), página por tenant com todos os estados §9.3, controle end-to-end (form POST → OperatorConsole → signal + audit), offboarding derruba a sessão no próximo request (403) |
| **Popula `tenant_config` p/ fairness (WS-B)** | `dse_platform/tenant_isolation.fairness_key` + `tenant_config` (Fase 1) | fairness key namespaced por tenant (`tenant::<id>`), lida pelo WS-B; a suíte prova que não colide entre tenants |

### Princípios não-negociáveis nesta fase

- **P1 (deterministic-or-human):** toda decisão de enforcement (repo/mode/ação/
  admissão/cross-tenant) é comparação de conjuntos/strings em código — nenhum
  LLM. O gate de plano **nunca auto-aprova**: cascata de approver vazia levanta
  `NoApproverError` (`require_plan_approver`).
- **P3 (no producer approves own work):** a cascata de approvers do WS-B tem
  fallback nos `designated_approvers` do bundle; vazia = bloqueia.
- **P6 (decline-never-truncate):** enforcement falha limpo em fronteira
  (`AccessDenied`/`CrossTenantViolation`/`LoginDenied`), nunca meio-caminho.
- **P8 (evidence over assertion):** toda decisão consequente (upsert de bundle,
  negação de acesso, mudança de kill switch, offboarding, ação de operador,
  violação cross-tenant) grava audit via `dse_audit.emit`.

### Contratos de consumo (cross-workstream) da Fase 2

```python
# WS-A (ingest-gateway) e WS-D (model-gateway): checar admissão nos 4 escopos
from dse_platform import is_admission_blocked
block = is_admission_blocked(tenant_id, channel)      # None = admissível
if block: refuse(block.scope, block.reason)            # 'global'|'tenant'|'channel'

# WS-A/WS-C: repos permitidos; WS-B: modes e ações bloqueadas
from dse_platform import require_repo_allowed, check_mode_allowed, require_action_allowed
require_repo_allowed(tenant_id, "org/repo", channel=ch, work_item_id=wid)
require_action_allowed(tenant_id, "direct_merge_to_protected_branch", channel=ch)

# WS-B (gate de plano, WSB-E3-T2): cascata CODEOWNERS -> designated approvers
from dse_platform import require_plan_approver          # levanta NoApproverError se vazia
approvers = require_plan_approver(tenant_id, channel=ch, codeowners=owners, work_item_id=wid)

# WS-B (fairness worker-side): chave de namespacing por tenant
from dse_platform import fairness_key                   # "tenant::<id>", nunca colide

# WS-C: enforcement tenant-scoped de skills/retrieval (nega + audita cross-tenant)
from dse_platform import fetch_skill_scoped, query_retrieval_scoped
```

### SSO — como plugar um IdP real

O console valida `id_token` OIDC (RS256) contra o JWKS do IdP. Config por env
(ver `queue_board/asgi.py`):

```
DSE_OIDC_ISSUER=https://login.cliente.com
DSE_OIDC_AUDIENCE=dse-admin-console          # client_id
DSE_OIDC_JWKS_FILE=/etc/dse/idp-jwks.json    # ou DSE_OIDC_JWKS='{"keys":[...]}'
DSE_CONSOLE_SESSION_SECRET=<>=32 bytes>
```

Sem essas vars o login fica desabilitado (503) — apropriado para um deployment
sem IdP ainda. Em dev/teste, `dse_platform.dev_idp.DevIdP` mina id_tokens + JWKS
para exercitar o mesmo verifier (ver `tests/test_sso.py`).

### Gaps honestos (fixture/mock ou dependência externa — Fase 2)

- **IdP real (Keycloak/Okta/Entra/Ping) não provisionado nesta sessão.** O
  contrato OIDC (assinatura RSA + `iss/aud/exp`) é exercitado de verdade contra
  o `DevIdP` (keypair RSA real, `PyJWT` + `cryptography`). Para produção: apontar
  `OIDCVerifier` para o `jwks_uri` do IdP e trocar o handler `/login` por um
  redirect OIDC (authorization code flow) — verify/sessão/offboarding não mudam.
  **SAML** entra via broker OIDC (Keycloak/Dex/oauth2-proxy) na frente — o
  console não parseia SAML (ver ADR-22 §1). **SCIM** real (provisioning
  automático de papéis) é integração por cliente; o schema
  (`dse_console_identity.roles`) já suporta, o endpoint SCIM não está incluído.
- **Account matching SSO × chat/VCS não unificado** — o CHECK
  `platform IN ('slack','github','jira')` do `identity_links` da fundação
  (0001, não editável nesta fase) impede gravar `platform='sso'`. Principais de
  SSO são criados direto em `principals` via `sso.ensure_sso_principal`, com o
  matching em `dse_console_identity.sso_subject`. Documentado como dívida no
  ADR-22 §2 — resolvível quando a fundação adicionar `'sso'` ao CHECK.
- **Envio real de signals do queue board (`TemporalSignalSender`)** exige
  `temporalio` (extra `[temporal]`) e um workflow vivo. Os testes usam
  `FakeSignalSender` (marcado) — o caminho de validação + audit + estado durável
  (quarentena/kill switch) é 100% real; só o transporte do signal é fake, para
  não exigir um workflow por teste. Rodar contra o Temporal real na consolidação.
- **Custo corrente do budget** (`get_tenant_budget.spent_usd`) é agregado das
  linhas de audit com `details->>'cost_usd'` (ex. `coder_turn_completed`) — a
  mesma fonte que o OTel collector consome. É uma aproximação honesta enquanto
  nenhum provider real grava custo (mesma limitação da Fase 1; sem conta
  AWS/Bedrock — WS-D usa o tier `eco/echo-model` local, custo zero).
- **UI sem design system, por decisão (WSF-E6-T3):** HTML cru montado em Python,
  tabelas, forms POST. É ferramenta de operação, não produto. `python-multipart`
  é a única dependência nova só-para-forms.
- **Kill switch de canal escreve na tabela `channel_kill_switches` do WS-A**
  (data-plane, mesmo banco) — não editamos o arquivo/migração do WS-A. O composto
  `is_admission_blocked` lê global (nosso) → tenant (nosso) → canal (WS-A).

### Novas dependências (Fase 2)

`PyJWT>=2.8`, `cryptography>=42` (verificação OIDC RS256), `python-multipart`
(forms do queue board), extra opcional `temporalio>=1.7` (`[temporal]`, envio
real de signals). Reinstalar: `pip install -e "services/platform[temporal]"`.

### Rodar o queue board localmente

```bash
# via docker (fragment WS-F já declara o serviço queue-board na 8890)
#   NÃO rode make up (derrubaria a infra dos outros agentes) — build só este:
docker compose -f docker-compose.yml -f docker-compose.wsf.yml build queue-board
docker compose -f docker-compose.yml -f docker-compose.wsf.yml up -d queue-board
# ou direto com uvicorn (venv do WS-F):
uvicorn dse_platform.queue_board.asgi:app --port 8890
```

### Estrutura adicionada (Fase 2)

```
services/platform/
  dse_platform/
    access_bundles.py        (WSF-E3-T2)
    sso.py                    (WSF-E3-T3 — OIDC verify, login, offboard)
    dev_idp.py                (WSF-E3-T3 — IdP OIDC de dev, fixture)
    steering_resolution.py    (WSF-E3-T3 — offboarding × steering)
    kill_switches.py          (WSF-E6-T2 — 4 escopos + quarentena)
    tenant_isolation.py       (WSF-E4-T3 — enforcement camada a camada)
    queue_board/
      api.py                  (WSF-E6-T1 — projeção §9.3, budgets, trilha)
      signals.py              (WSF-E6-T2 — SignalSender real/fake)
      operator.py             (WSF-E6-T2 — controles + audit por operador)
      app.py                  (WSF-E6-T3 — FastAPI + HTML mínimo, SSO gate)
      asgi.py                 (entrypoint uvicorn na 8890)
  Dockerfile                  (imagem do queue board)
  tests/
    test_access_bundles.py  test_sso.py  test_kill_switches.py
    test_tenant_isolation.py  test_queue_board.py  test_queue_board_app.py

migrations/0013_wsf2.sql      (dse_access_bundle, dse_console_identity,
                               dse_kill_switch_global, dse_work_item_quarantine)
infra/ADR-22-identity.md      (design doc SSO/SCIM/offboarding)
docker-compose.wsf.yml        (+ serviço queue-board na 8890)
```

---

## Fase 3 — o que foi adicionado

WS-F Fase 3 ("Evidence"): **ADR-28 completo** (rotação agendada de secrets +
secrets de preview via ESO), **retenção por classificação de dados**
(WSF-E8-T2/§12.2) e **ativação do alerta de history do Temporal**
(ALERTING-RULES §3). Tudo aditivo sobre Fases 1+2. Migração reservada:
`migrations/0018_wsf3.sql`. Nenhuma porta nova.

### Mapa de entrega (Fase 3)

| Tarefa | Onde | Prova |
|---|---|---|
| **WSF-E2-T3b(a) — rotação AGENDADA de secrets de serviço** | `dse_platform/secret_rotation.py` (`rotate_secret`/`rotate_from_manifest`) + `dse_platform/jobs_scheduler.py` + serviço `platform-jobs` no `docker-compose.wsf.yml` | `tests/test_secret_rotation.py` — contra o Vault REAL: **leitor ativo concorrente em loop durante 5 rotações = zero janela de erro** (aceite literal da tarefa), 1 audit row por rotação (`service_secret_rotated`) SEM nunca vazar o material, generator igual/vazio recusado (P6), manifest isola falhas, entrypoint agendado exercitado. Rodado também DENTRO do container (`docker exec dse_platform_jobs python -m dse_platform.jobs_scheduler --once` → rotacionou `dse/service/queue-board-session` v1→v2) |
| **WSF-E2-T3b(b) — secrets de preview via ESO** | `infra/k8s-local/setup-eso.sh` (ESO **2.8.0 pinado** via helm, instalado DE VERDADE no k3d `dse-preview`) + `infra/k8s-local/eso/*.yaml` (ClusterSecretStore `dse-vault` + exemplo) | `tests/test_eso_preview_secrets.py` — Secret k8s **materializa num namespace de preview a partir do Vault do compose** (rede dse_net, `http://vault:8200`), rotação no Vault se propaga no refreshInterval, e **teste negativo de escopo**: ExternalSecret apontando para `dse/service/*` NUNCA fica Ready (token do ESO é escopado por policy `dse-preview-read` a `secret/data/dse/preview/*` — nenhum root token entra no cluster) |
| **WSF-E8-T2 — retenção por classificação** | `dse_platform/retention.py` + `migrations/0018_wsf3.sql` (`tenant_config.retention` JSONB + índice em `ingest_events.received_at`) | `tests/test_retention.py` (16 testes, Postgres real) — política por tenant/classe com validação de shape, anonimização de `ingest_events.payload` (tombstone; só `processed`, só a classe, idempotente), expurgo de `wse_artifacts` (JOIN `work_items` p/ data_class; **quarentenado nunca expurgado**; chaves deletadas no audit row p/ cleanup compensatório no Garage), dry-run sem mutação, **audit_log recusado como alvo em código** + linhas antigas de audit sobrevivem, falha de 1 tenant não aborta a sweep (auditada como `retention_failed`) |
| **ALERTING-RULES §3 ATIVADA — history do Temporal** | `infra/otel-collector-config.yaml` (pipeline `metrics/history_alert`: `filter` OTTL + `transform` severity + exporter `debug/history_alert`) | `tests/test_history_alert.py` — OTLP real contra o collector da fundação: acima do threshold aparece no canal de alerta com `dse.alert_severity=warning|critical` corretos (eventos E bytes), abaixo do threshold e métricas não-history NUNCA vazam |

### Decisão P7 (pedida pelo aceite): scheduler Python no compose, não CronJob no k3d

Justificativa completa no docstring de `dse_platform/jobs_scheduler.py`. Curto:
os consumidores dos secrets de serviço (adapters/gateway/broker) e o Postgres
alvo da retenção vivem no docker-compose — agendar no cluster criaria
dependência cruzada de runtime sem ganho. O MESMO módulo roda como CronJob em
K8s real (`python -m dse_platform.jobs_scheduler --once` — testado). Temporal
Schedules foi rejeitado por acoplar a rotação à disponibilidade de um
consumidor indireto dela.

### Contratos de consumo (cross-workstream) da Fase 3

```python
# WS-E (previews, WSE-E4-T10): secrets de preview via ESO —
#   secretStoreRef: { kind: ClusterSecretStore, name: dse-vault }
#   paths no Vault: secret/dse/preview/<...>   (KV v2, mount "secret")
# (exemplo vivo: kubectl -n dse-preview-example get externalsecret)

# WS-E (lifecycle do artifact store): política de retenção é fonte única daqui
from dse_platform import get_retention_policies, set_retention_policy, run_retention

# rotação para qualquer serviço que precise trocar um secret interno
from dse_platform import rotate_secret
rotate_secret("dse/service/meu-secret", actor="system:secret-rotator")
```

### Pedidos registrados para outros workstreams (não editamos nada deles)

- **WS-E**: (1) `GRANT DELETE ON wse_artifacts TO dse_app` na integração —
  até lá o expurgo real de artifacts reporta `skipped` com razão explícita
  (dry-run/contagem funciona; o teste prova o expurgo com conexão
  privilegiada); (2) consumir `purged_store_keys` do audit row
  `retention_executed` para deletar os objetos correspondentes no Garage.
- **WS-B**: pinar o nome canônico da métrica de history (o filtro aceita
  `dse.workflow.history_length`/`history_size_bytes` e as variantes
  `temporal_workflow_event_history_*` — ver ALERTING-RULES §3 atualizado).
- **Fundação**: promover o nome da métrica a `dse_contracts.constants`
  (`OTEL_METRIC_HISTORY_LENGTH`) na próxima janela de contrato.

### Gaps honestos (Fase 3)

- **Rotação de credenciais de PROVEDOR externo** (Slack bot/GitHub App/Jira):
  o mecanismo (versionamento KV v2 + verificação + audit + agenda) está
  completo e provado; emitir a credencial nova na API do provedor exige apps
  reais (pendência administrativa herdada) — é implementar um `Generator`
  por integração (interface documentada em `secret_rotation.py`).
- **Vault continua dev-mode** (fundação, Fases 1-3): o hardening do deploy
  (HA/auto-unseal) é infra de produção — o caminho ESO/policies/tokens
  escopados já é o de produção. `setup-eso.sh --rotate` troca o token do ESO.
- **Alerta de history**: canal MVP é o stdout do collector (linha com
  `dse.alert=...`); upgrade para Alertmanager documentado em
  ALERTING-RULES §3. A emissão CONTÍNUA da métrica é do WS-B (em paralelo
  nesta fase); o teste prova o pipeline com OTLP real emitido pelo teste.
- **k3d/ESO indisponíveis** ⇒ os testes de ESO skipam com razão explícita
  (mesmo padrão do egress-proxy na Fase 1) — rode
  `infra/k8s-local/setup-k3d-argocd.sh` e `infra/k8s-local/setup-eso.sh`.

### Rodar (Fase 3)

```bash
source .venv-wsf/bin/activate   # mesmo venv das fases anteriores
export DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse \
       DSE_AUDIT_DATABASE_URL=postgresql://dse_app:dse_app_dev_only@localhost:5432/dse \
       DSE_PLATFORM_DATABASE_URL=postgresql://dse_app:dse_app_dev_only@localhost:5432/dse \
       VAULT_ADDR=http://localhost:8200 VAULT_DEV_ROOT_TOKEN=dse_dev_root
python scripts/migrate.py                      # aplica 0018_wsf3.sql
./infra/k8s-local/setup-eso.sh                 # ESO 2.8.0 + SecretStore + exemplo
pytest -q packages/dse_audit services/platform # 121 passed, 2 skipped

# jobs agendados (compose)
docker compose -f docker-compose.yml -f docker-compose.wsf.yml up -d platform-jobs
docker exec dse_platform_jobs python -m dse_platform.jobs_scheduler --once  # modo CronJob
```

### Estrutura adicionada (Fase 3)

```
services/platform/
  dse_platform/
    secret_rotation.py       (WSF-E2-T3b(a) — rotação sem downtime + audit)
    retention.py              (WSF-E8-T2 — política por classe + expurgo/anonimização)
    jobs_scheduler.py         (agendador; compose service OU CronJob --once)
  tests/
    test_secret_rotation.py  test_retention.py
    test_eso_preview_secrets.py  test_history_alert.py

migrations/0018_wsf3.sql      (tenant_config.retention + índice received_at)
infra/k8s-local/setup-eso.sh  (ESO 2.8.0 pinado + policy/token escopados)
infra/k8s-local/eso/          (ClusterSecretStore dse-vault + exemplo de preview)
infra/otel-collector-config.yaml  (+ pipeline metrics/history_alert — §3 ATIVA)
docker-compose.wsf.yml        (+ serviço platform-jobs)
```

## Fase 4 — o que foi adicionado (loop hardening & learning)

A Fase 4 do WS-F é o **pacote do pilot gate de segurança** + a decisão de escopo Webex. Nada de
código de plataforma novo em `dse_platform/` — a Fase 4 do WS-F é **documentação de segurança
formal + uma suíte de red-team executável** que ATACA os controles já construídos (não os
reescreve).

### Mapa de entrega (Fase 4)

| Tarefa | Entregável | Estado |
|---|---|---|
| **WSF-E8-T1** threat model + data-flow | `infra/THREAT-MODEL.md` — matriz ameaça→controle→teste por componente + diagramas mermaid Tier 1 (PrivateLink) / Tier 2 (air-gapped) | Completo; cada linha cita arquivo+teste reais; gaps honestos listados (§4) |
| **WSF-E8-T3** programa de red-team | `infra/RED-TEAM-PROGRAM.md` (dono/cadência/escopo/itens manuais) + `services/platform/tests/test_red_team.py` (21 ataques executáveis) | Completo; 21/21 passando contra infra real |
| **WSF-E5-T3** topologia B | `infra/helm/dse/values-topology-b.yaml` + `templates/model-server.yaml` + `infra/helm/dse/TOPOLOGY-B.md` (custo NFR-08 × N) | Completo; `helm lint`+`template` validam A e B |
| **Decisão Webex (ADR-25)** | `infra/ADR-25-webex-decision.md` — de-scope formal com sign-off + como reverter | Completo (pendente ratificação arquiteto/stakeholder) |

### Suíte de red-team (`tests/test_red_team.py`) — o que ela ATACA de verdade

Ataques contra controles REAIS (não mocks), com skip claro se o controle-alvo não estiver no
ambiente (P6/P8 — "não pude verificar" > falso-positivo):

- **`TestForgedWebhook`** → HMAC de `ingest_gateway.security` (WS-A): assinatura forjada / chave
  errada / ausente / replay fora da janela recusados; controle positivo prova que não é "sempre
  nega".
- **`TestPromptInjection`** → `ingest_gateway.sanitize` (unicode invisível/bidi + secret plantado)
  E a contenção real: egress default-deny (:8806) recusa exfiltração para pastebin/telegram/cloud
  metadata + bypass por confusão de host.
- **`TestCrossTenant`** → `dse_platform.tenant_isolation`: A lê skill/retrieval/audit/token de B →
  `CrossTenantViolation` + linha `cross_tenant_access_denied` no audit; path traversal de artifact
  bloqueado.
- **`TestMaliciousSkill`** → `sandbox_runtime.skill_promotion` (WS-C, liga com WSC-E4-T3): candidate
  tenta virar `active`/`approved` sem aprovador humano → `ApproverRequired`; candidate nunca é
  servida ao Planner (`read_approved_skills`).

Como rodar (venv do WS-F, infra no ar):
```bash
export DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse
pytest -q services/platform/tests/test_red_team.py     # 21 passed
```

### Validar as topologias do Helm (Fase 4)
```bash
helm lint infra/helm/dse
helm lint infra/helm/dse -f infra/helm/dse/values-topology-b.yaml
helm template dse-acme infra/helm/dse                                              # topologia A
helm template dse-acme infra/helm/dse -f infra/helm/dse/values.yaml \
    -f infra/helm/dse/values-topology-b.yaml                                       # topologia B
```
Topologia B liga o `model-server` air-gapped in-cluster (GPU), força Postgres/Temporal/Vault
self-hosted e allowlist de egress só-interna. Custo operacional documentado em `TOPOLOGY-B.md`
(NFR-08 × N — sem amortização, a GPU dedicada por cliente é o driver dominante).

### Gaps honestos (Fase 4)

- **Credenciais reais** (GitHub App/Slack/Jira/AWS-Bedrock) continuam ausentes — assinatura e
  PrivateLink têm a lógica de produção mas rodam com segredo de env/fixture/echo. Pilot gate
  administrativo (adendo 03 §Parte 3).
- **Supply-chain**: sem SBOM/assinatura de imagem/scan de CVE no CI — item manual de maior
  prioridade do RED-TEAM-PROGRAM (§5).
- **Replay de credencial contra upstream real** e **console sem IdP real** permanecem itens
  manuais do programa (documentados em §5, não escondidos).
- **`model-server` air-gapped** é empacotamento validado (lint/template); a imagem de serving real
  é P2 (WSD-E5-T2/T3), não bloqueia piloto.

### Estrutura adicionada (Fase 4)

```
infra/THREAT-MODEL.md                    (WSF-E8-T1 — matriz + data-flow Tier 1/2)
infra/RED-TEAM-PROGRAM.md                (WSF-E8-T3 — dono/cadência/escopo/manuais)
infra/ADR-25-webex-decision.md           (de-scope formal + reversão)
infra/helm/dse/values-topology-b.yaml    (WSF-E5-T3 — overlay estrito)
infra/helm/dse/templates/model-server.yaml  (model air-gapped, gated modelServer.enabled)
infra/helm/dse/TOPOLOGY-B.md             (custo operacional NFR-08 × N)
infra/helm/dse/values.yaml               (+ bloco modelServer, disabled por default — A intacta)
services/platform/tests/test_red_team.py (21 ataques executáveis)
```

Sem migração nova na Fase 4 (WS-F): a suíte de red-team reusa as tabelas existentes
(`skill_registry`/`skill_episode` da 0019, `virtual_keys`, `audit_log`). `0020_wsf4.sql` ficou
reservada mas não foi necessária.
