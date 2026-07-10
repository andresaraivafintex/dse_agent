# services/platform — WS-F (Segurança, compliance, plataforma e operações)

Implementação Fase 1 dos P0 do WS-F. Lê `../../CONVENTIONS.md` primeiro se
ainda não leu — este README assume o vocabulário/contratos de lá.

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
