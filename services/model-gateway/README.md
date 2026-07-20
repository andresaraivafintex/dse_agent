# WS-D — model-gateway (LiteLLM)

Gateway de modelos in-VPC do Fintex DSE. Todo consumo de LLM por qualquer
sessão de agente (Coder na Fase 1; Planner/Tester/Reviewer na Fase 2) passa
por aqui — nunca um SDK de provider direto (`anthropic`, `boto3`, `openai`).
Contrato de consumo já publicado pela fundação:
`dse_contracts.gateway_contract.{GatewayCallHeaders,GatewayErrorResponse,Stage}`.

## O que está implementado e funcionando (testado contra infra REAL, sem mocks)

- **LiteLLM proxy real rodando em Docker** (`docker-compose.wsd.yml`, porta
  4000), imagem pinada por **digest** (não por tag flutuante `main-latest`):
  `ghcr.io/berriai/litellm@sha256:4c76cc4f47b72c82194f2774f458cc92de369ac6439b236757f0f69b71392722`
  (litellm==1.93.0, puxada em 2026-07-09). Config em `litellm_config.yaml`.
- **3 models registrados no LiteLLM** e confirmados via `GET /v1/models`:
  `bedrock/anthropic.claude-3-5-sonnet`, `bedrock/anthropic.claude-3-haiku`
  (placeholders de infra, ver abaixo) e **`eco/echo-model`** (funcional
  agora).
- **"Modelo eco"** (`echo_provider/server.py`): servidor HTTP OpenAI-compatible
  escrito do zero, só stdlib (`http.server`), determinístico (mesma entrada
  → mesma saída sempre, sem RNG/relógio), rodando como container próprio
  (`model-gateway-echo`) na rede `dse_net`, registrado no LiteLLM como
  provider `openai/echo-model` via `api_base`. Prova o gateway fim-a-fim sem
  nenhuma API paga/externa.
- **Virtual keys de verdade**: `mint_virtual_key(tenant_id, work_item_id,
  stage)` / `revoke_virtual_key(key)` chamam a API nativa do LiteLLM
  (`POST /key/generate` / `POST /key/delete`) contra um Postgres **dedicado**
  (`dse_litellm`, mesma instância, banco separado do schema compartilhado
  `dse` — criado com `CREATE DATABASE dse_litellm OWNER dse;`, migrado
  automaticamente pelo próprio LiteLLM via Prisma no primeiro boot).
  Testado: mint → chamada com a virtual key funciona → revoke → chamada
  seguinte recebe `401` de verdade do proxy real.
- **Model-scoping de virtual keys** confirmado: uma key emitida com
  `models=["eco/echo-model"]` recebe `403` ao tentar chamar
  `bedrock/anthropic.claude-3-haiku`.
- **Tabela `virtual_keys`** (`migrations/0005_wsd.sql`, aplicada no Postgres
  da fundação): registro do lado do DSE de toda key emitida/revogada por
  tenant/work_item/stage, com `key_hash` (sha256, não guarda a key em claro)
  para permitir lookup em `revoke_virtual_key` sem persistir segredo.
- **Audit ledger (P8)**: `virtual_key.issued`, `virtual_key.revoked`,
  `virtual_key.issue_failed`, `virtual_key.revoke_failed` — todas via
  `dse_audit.emit(actor="system:model-gateway", ...)`, nunca INSERT direto.
  Confirmado com query real no `audit_log` particionado.
- **Master key do LiteLLM lida do Vault dev real** (`localhost:8200`, KV v2,
  path `secret/data/model-gateway/master-key`) com fallback para env var
  `DSE_LITELLM_MASTER_KEY` se Vault não estiver acessível — não é fixture,
  é uma leitura HTTP real contra o Vault da fundação (ver `settings.py`).
- **Instrumentação OTel (WSD-E3-T1)**: cada `chat_completion` gera um span
  `dse.model_gateway.chat_completion` com os atributos do contrato
  (`dse.tenant_id/work_item_id/stage/model/cost_usd/tokens_in/tokens_out`),
  preenchidos com custo/tokens **reais** que o LiteLLM devolve (header
  `x-litellm-response-cost-original` + `usage` do body) — nunca recalculados
  por nós. Span também marca status ERROR em falhas (denial visível em
  observabilidade).
- **Export de custo (WSD-E3-T2)**: `cost_export.aggregate_cost()` agrega por
  `(tenant_id, task_class, stage)` a partir dos spans; `export_api.py`
  expõe isso como 2 rotas FastAPI (`GET /internal/cost-export`,
  `GET /internal/cost-export/by-tenant`); `scripts/cost_export_cli.py` é a
  versão de linha de comando.
- **`model_gateway_client` publicado como biblioteca Python instalável**
  (`pyproject.toml` próprio) — é isso que o `sandbox_runtime` (WS-C) importa.
- **Teste de conformidade (WSD-E1-T4)**: prova estática (AST — nenhum
  `import boto3/anthropic/openai` em nenhum arquivo do pacote) + prova
  dinâmica (intercepta toda chamada `httpx.post` durante um fluxo real
  mint→call→revoke e confirma que 100% delas foram para a base URL do
  gateway, nunca para outro host).
- **Smoke test de "upgrade simulado" (WSD-E1-T1)**:
  `scripts/smoke_test.py` grava uma baseline determinística (resposta do
  modelo eco) e compara byte-a-byte contra ela — o procedimento de upgrade
  documentado no topo do próprio script.

## Fase 2 ("Judgment & queue") — o que foi adicionado (WSD-E2/E3-T4/E4/E5)

Tudo abaixo é ADITIVO sobre a Fase 1 (as 20 chamadas/superfícies da Fase 1
continuam idênticas). O enforcement é **permissivo por default**: sem política,
sem cap, sem kill switch e sem reassign configurados, `chat_completion` se
comporta exatamente como na Fase 1. Migração: `migrations/0011_wsd2.sql`
(`model_policies`, `model_call_ledger`, `work_item_budgets`,
`gateway_kill_switches`, `model_reassignments`).

### WSD-E2-T1 — Motor de política per-stage/per-tenant (`policy.py`)
- Config **declarativa** (fora do código do agente) na tabela `model_policies`,
  mapeando `(tenant, stage, data_class, risk_class) -> {allowed_models,
  preferred_model}`. Coringa `'*'` em qualquer dimensão; a linha mais
  **específica** (menos coringas), desempatada por `priority`, vence. Sem linha
  aplicável -> allow-all (Fase 1 preservada).
- **Hot-reload sem redeploy**: o motor lê a tabela no call time com um cache TTL
  curto (`DSE_POLICY_CACHE_TTL_SECONDS`, default 5s). Um operador dá
  `INSERT/UPDATE` e o efeito aparece em <=TTL segundos. `load_policies_from_file`
  carrega um YAML/JSON declarativo ("config as code") para a tabela.
- **Deny tipado**: chamada a modelo não permitido -> `GatewayCallError` (HTTP
  403) com corpo `GatewayErrorResponse{error="policy_denied"}` + linha de audit
  `gateway.call_denied_policy` (P8). O workflow do WS-B converte em Failed (P6).
- **Integração com o access bundle do WS-F** (`dse_access_bundle`): se o bundle
  default do tenant existe e está `enabled=false`, o tenant está desligado
  (deny-all). Leitura defensiva — se a tabela do WS-F ainda não existir, degrada
  para "sem restrição adicional".
- Nota: `risk_class` é dimensão da tabela mas hoje é sempre `'*'` na resolução
  porque `GatewayCallHeaders` (contrato da fundação) ainda não carrega um header
  de risco — quando carregar, o motor já casa a dimensão sem mudança de schema.

### WSD-E2-T2 — Enforcement de budget no call time (`budget.py`)
- Dois caps checados a **cada** chamada: budget de runtime do WorkItem
  (`work_item_budgets` ou `per_task_usd` do access bundle) e budget **agregado**
  do tenant no mês (`tenant_config.monthly_budget_usd` do WS-F ou `monthly_usd`
  do access bundle). "spent-so-far" vem do **ledger durável** (não de contador
  em memória).
- Exaustão -> recusa limpa na fronteira (P6): `GatewayCallError` (HTTP 402)
  `GatewayErrorResponse{error="budget_exhausted"}` + audit
  `gateway.call_denied_budget`.

### WSD-E4-T2 — Kill switch por escopo + reassign de modelo em voo (`controls.py`, `control_api.py`)
- Kill switch de **4 escopos** (global | tenant | work_item | channel). O
  gateway enforça no call time os escopos visíveis nos headers
  (global/tenant/work_item); `channel` fica na tabela para operabilidade mas é
  honrado na admissão pelo WS-A/WS-B (o gateway não vê o canal).
- **Conecta aos controles do WS-B/WS-F**: o check lê TAMBÉM
  `dse_kill_switch_global` (WS-F) e `tenant_config.kill_switch_enabled` (WS-F),
  então acionar por qualquer caminho zera as chamadas do escopo. Efeito **<60s**:
  cache TTL curto (default 5s), muito abaixo de 60s (não interrompe uma geração
  em curso — não há stream aqui — zera a emissão de novas chamadas do escopo).
- **Reassign de modelo em voo**: um operador troca o modelo efetivo de um
  WorkItem; a próxima chamada usa `to_model` no lugar do requisitado. Reassign
  **não burla a política** (o modelo efetivo ainda passa pelo policy engine).
- `control_api.py` é o FastAPI de operador (`uvicorn
  model_gateway_client.control_api:app --port 4010`): `POST /internal/kill-switch`,
  `POST /internal/reassign-model`, `DELETE /internal/reassign-model/{wi}`,
  `GET /internal/budget-status`, `GET /internal/policy`. Toda mutação já emite
  audit via `controls.py`.

### WSD-E3-T4 — Agregação de custo em fonte DURÁVEL (`ledger.py`)
- Cada chamada bem-sucedida grava uma linha em `model_call_ledger` com o custo/
  tokens **reais** do LiteLLM. `cost_export.aggregate_cost(source="ledger")` (o
  novo **default**) lê dessa tabela — **sobrevive a restart** e agrega entre
  processos (resolve a pendência #4 do adendo). `source="memory"` mantém o
  caminho legado (spans em memória) para testes de unidade puros.
- O **OTel collector do WS-F já está no ar** (`dse_otel_collector`, OTLP em
  `localhost:4317/4318`) — provado nesta sessão: com
  `DSE_OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces` os spans
  `dse.model_gateway.chat_completion` chegam ao collector (visíveis no
  `docker logs dse_otel_collector`). O collector local só faz `debug`/stdout
  (sem backend consultável), por isso a agregação **consultável** mora no ledger
  Postgres; os spans no collector são para dashboards/alerting do WSF-E7.
- `OTEL_ATTR_TASK_CLASS` agora vem de `dse_contracts.constants` (promovido na
  fundação da Fase 2); `telemetry.py` re-exporta para compatibilidade.

### WSD-E5-T1 — Suite de avaliação Tier-2 (`eval_suite/`) — **dono: WS-D**
- Harness real e rodável (`python -m model_gateway_client.eval_suite`) que
  dispara prompts de referência (`eval_suite/cases.yaml`) contra os modelos
  configurados e reporta pass/fail + custo + latência por caso. Modelos
  indisponíveis na infra atual (ex.: `bedrock/*` sem AWS) viram **SKIP**, não
  falha. Exit code 0 se nada falhou (gate de CI/promoção de modelo). **Não é** o
  Tier-2 air-gapped serving completo (Fase 4) — é a estrutura mínima de eval
  (gap 6) com dono nomeado.

## API pública estável (`model_gateway_client`)

```python
from model_gateway_client import mint_virtual_key, revoke_virtual_key
from model_gateway_client import chat_completion, ChatCompletionResult
from dse_contracts.gateway_contract import GatewayCallHeaders, Stage

key = mint_virtual_key(tenant_id, work_item_id, Stage.coder, models=["eco/echo-model"])

result = chat_completion(
    headers=GatewayCallHeaders(tenant_id=tenant_id, work_item_id=work_item_id, stage=Stage.coder),
    virtual_key=key,
    model="eco/echo-model",
    messages=[{"role": "user", "content": "..."}],
)
# result.content / result.cost_usd / result.tokens_in / result.tokens_out

revoke_virtual_key(key)
```

**`sandbox_runtime` (WS-C) deve importar exatamente essas duas funções** de
`model_gateway_client` para o lifecycle de key por sessão de Coder — a
assinatura é estável e não deve mudar sem coordenação:

```python
def mint_virtual_key(
    tenant_id: str,
    work_item_id: str,
    stage: Stage | str,
    *,
    models: list[str] | None = None,
    max_budget_usd: float | None = None,
    ttl_seconds: int | None = None,
) -> str: ...

def revoke_virtual_key(key: str) -> None: ...
```

## O que é fixture/mock local (e por quê)

- **Bedrock/PrivateLink**: não há conta AWS disponível nesta sessão de
  desenvolvimento. `litellm_config.yaml` registra os 2 models Bedrock com
  `litellm_params` reais (mesmo shape que produção usaria) mas os valores
  (`DSE_AWS_REGION`, `DSE_BEDROCK_PRIVATELINK_ENDPOINT`,
  `DSE_BEDROCK_IAM_ROLE_ARN`) são **placeholders** — ver
  `docker-compose.wsd.yml`. O LiteLLM registra o model_list sem erro (a
  validação de credencial só acontece na primeira chamada real), então dá
  pra ver os 2 aliases em `GET /v1/models` mesmo sem AWS, mas uma chamada de
  verdade para `bedrock/*` falharia (esperado) até a infra real existir.
- **Modelo eco**: não é um LLM de verdade — é um double determinístico. É
  "real" no sentido de ser um servidor HTTP de verdade rodando num container
  de verdade, falado via HTTP real pelo LiteLLM real — só a "inteligência"
  do modelo é uma transformação de string, documentado exatamente assim em
  `echo_provider/server.py`.
- **Contagem de tokens do modelo eco**: `len(text.split())` (whitespace) —
  não é um tokenizer BPE real. Suficiente para provar que custo/tokens
  fluem ponta a ponta pelo pipeline de observabilidade; não é uma métrica de
  produção.
- **`cost_export`**: agrega a partir de um `InMemorySpanExporter` (buffer
  do processo atual). Funciona perfeitamente para os testes e para
  demonstração local, mas não sobrevive a restart nem agrega entre
  processos — ver "O que falta para produção".

## O que falta para produção

1. **Bedrock/PrivateLink real**: substituir os 3 placeholders em
   `docker-compose.wsd.yml` (`DSE_AWS_REGION`, `DSE_BEDROCK_PRIVATELINK_ENDPOINT`,
   `DSE_BEDROCK_IAM_ROLE_ARN`) pelos valores reais provisionados pelo time de
   infra do cliente (VPC endpoint da PrivateLink para `bedrock-runtime`, IAM
   role assumível via IRSA/instance profile — nunca access key estática).
2. **Vault de produção**: hoje lemos do Vault **dev** da fundação
   (`localhost:8200`, root token). Em produção isso deveria vir de
   `services/platform/` (WS-F) com um client compartilhado, políticas Vault
   escopadas (não root token), e rotação. `settings.py` já isola esse
   detalhe atrás de `litellm_admin_master_key()` — trocar a implementação
   ali não deveria afetar `virtual_keys.py`/`gateway_call.py`.
3. **Banco do LiteLLM em produção**: hoje é `dse_litellm` na mesma instância
   Postgres de dev. Em produção deveria ser uma instância gerenciada própria
   (RDS dedicado), não compartilhando hardware com o control-plane.
4. **Export de custo real**: hoje é um buffer em memória por processo.
   Produção precisa do OTel collector do WS-F recebendo via OTLP
   (`DSE_OTEL_EXPORTER_OTLP_ENDPOINT` já suportado em `telemetry.py`, só
   falta o collector existir) e `cost_export._iter_spans()` devendo virar
   uma query nesse backend em vez do buffer local — a função
   `aggregate_cost()` já é a interface estável para essa troca.
5. **`OTEL_ATTR_TASK_CLASS`**: usei um atributo extra (`dse.task_class`,
   `model_gateway_client.telemetry.OTEL_ATTR_TASK_CLASS`) que não existe em
   `dse_contracts.constants` hoje (o contrato publicado só tem
   TENANT/WORK_ITEM/STAGE/MODEL/COST_USD/TOKENS_IN/TOKENS_OUT). Não editei
   `packages/contracts` (fora do meu escopo) — mas para a agregação por
   task-class pedida em WSD-E3-T2 fazer sentido fora deste processo, esse
   atributo deveria subir para o contrato compartilhado da próxima vez que
   alguém tocar em `dse_contracts.constants`.
6. **Enforcement non-bypassable no servidor** (Fase 2 entregue com uma
   ressalva honesta): o policy/budget/kill-switch/reassign de `enforcement.py`
   roda no caminho do **cliente** do gateway (`chat_completion`). O backstop
   server-side já existe e NÃO é burlável pelo sandbox — as virtual keys são
   escopadas por modelo no LiteLLM (403 nativo, testado) e podem levar
   `max_budget`/`duration`. Para um deployment 100% non-bypassable, o mesmo
   `enforce_call` deve ser espelhado como um **pre-call hook do LiteLLM proxy**
   (custom callback carregado em `litellm_config.yaml` + rebuild da imagem
   pinada) — não feito nesta sessão para não tocar a imagem pinada por digest
   sem rodar o smoke de upgrade. A lógica é a mesma função; só muda o ponto de
   montagem.
7. **Rotação/expiração automática de virtual keys** — hoje só é revogada
   explicitamente via `revoke_virtual_key`. `ttl_seconds`/`max_budget_usd`
   já são aceitos por `mint_virtual_key` e repassados ao LiteLLM
   (`duration`/`max_budget`), mas o lifecycle "revogar automaticamente
   quando o work_item termina" é responsabilidade do `sandbox_runtime` (WS-C),
   não deste pacote.

## Como rodar a infra do WS-D

Infra compartilhada (Postgres/Temporal/Redis/Vault) já está no ar — não
rode `make up`/`make down`. Para subir SÓ os serviços do WS-D (usa a rede
externa `dse_net` já criada pela fundação, não afeta os outros containers):

```bash
# banco dedicado do LiteLLM (uma vez só, idempotente)
docker exec dse_postgres psql -U dse -d dse -c "CREATE DATABASE dse_litellm OWNER dse;" || true

docker compose -f docker-compose.wsd.yml up -d --build
```

Aplicar a migração `0005_wsd.sql` (idempotente):

```bash
DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse python3 scripts/migrate.py
```

## Como rodar os testes

```bash
python3.12 -m venv /Users/saraiva/Documents/DSE/fase1/.venv-wsd
source /Users/saraiva/Documents/DSE/fase1/.venv-wsd/bin/activate
cd /Users/saraiva/Documents/DSE/fase1
pip install -e packages/contracts -e packages/dse_audit -e packages/dse_identity
pip install -e services/model-gateway
pip install pytest

cd services/model-gateway
pytest -q
```

`tests/conftest.py` já assume os defaults do `docker-compose.wsd.yml`
(`http://localhost:4000`, master key de dev, Vault dev, Postgres da
fundação) via `os.environ.setdefault` — funciona out-of-the-box se a infra
acima estiver no ar.

### Resultado real (rodado nesta sessão)

Fase 1 (20 testes) + Fase 2 (19 testes) contra a MESMA infra real:

```
39 passed in 6.79s
```

Cobertura Fase 1: `echo_provider` isolado (determinismo, shape OpenAI, 404),
round-trip completo mint→call→revoke→denied contra o LiteLLM real,
model-scoping de virtual key (403), tabela `virtual_keys` (insert/revoke
real no Postgres), audit ledger (linhas reais gravadas/consultadas),
telemetria OTel (atributos do contrato + status ERROR em falha), agregação
de custo (multi-tenant, multi-task-class), e conformidade
gateway-only (estática + dinâmica).

Cobertura Fase 2:
- `test_policy_enforcement.py` — resolução por especificidade/coringa, default
  permissivo, deny tipado + audit numa chamada real negada, hot-reload muda a
  decisão sem redeploy, carga de política de YAML.
- `test_budget_enforcement.py` — custo real acumulado no ledger durável, deny de
  budget de WorkItem e de tenant (`tenant_config`) na fronteira + audit,
  `budget-status`.
- `test_kill_switch_reassign.py` — kill switch de work_item (liga/desliga),
  gateway honrando o kill switch de tenant do WS-F (`tenant_config`), reassign
  trocando o modelo efetivo (echo no lugar de haiku) + audit, reassign não
  burlando a política.
- `test_ledger_durable.py` — agregação durável sobrevive ao "clear" do buffer em
  memória (proxy de restart), isolamento por tenant.
- `test_eval_suite.py` — casos do echo passam, modelo indisponível vira SKIP.

Também verificado à mão nesta sessão (não em pytest): o export OTLP real chega
ao `dse_otel_collector` do WS-F (1 span `dse.model_gateway.chat_completion`
recebido) e o `python -m model_gateway_client.eval_suite` roda (3 pass, 1 skip).

## Version pinning e upgrade simulado (WSD-E1-T1)

Ver comentário no topo de `docker-compose.wsd.yml` e docstring de
`scripts/smoke_test.py`. Resumo: imagem sempre por digest, nunca por tag
flutuante; upgrade = trocar o digest + `docker compose ... up -d
--force-recreate model-gateway` + `pytest -q` + `scripts/smoke_test.py`
batendo limpo contra a baseline em `scripts/smoke_baseline.json` antes de
promover.

## Arquivos

```
migrations/0005_wsd.sql                        # tabela virtual_keys (Fase 1, raiz do repo)
migrations/0011_wsd2.sql                        # Fase 2: model_policies, model_call_ledger,
                                               #   work_item_budgets, gateway_kill_switches,
                                               #   model_reassignments (raiz do repo)
docker-compose.wsd.yml                          # serviços model-gateway + model-gateway-echo (raiz do repo)
services/model-gateway/
  litellm_config.yaml                           # config do LiteLLM (bedrock placeholders + eco real)
  pyproject.toml                                # pacote instalável model-gateway-client (+pyyaml)
  README.md
  echo_provider/
    server.py                                   # "modelo eco" — stdlib puro, determinístico
    Dockerfile
  model_gateway_client/
    __init__.py                                 # superfície pública estável (Fase 1 + Fase 2)
    settings.py                                  # env vars + leitura real do Vault dev
    db.py                                        # conexão Postgres (control-plane dse)
    virtual_keys.py                              # mint_virtual_key / revoke_virtual_key
    gateway_call.py                              # chat_completion (enforcement no call time + ledger)
    telemetry.py                                 # spans OTel (contrato dse_contracts.constants)
    cost_export.py                               # agregação de custo (default: ledger durável)
    export_api.py                                # FastAPI fino sobre cost_export
    errors.py
    # --- Fase 2 ---
    policy.py                                    # WSD-E2-T1 motor de política per-stage/per-tenant
    budget.py                                    # WSD-E2-T2 enforcement de budget no call time
    controls.py                                  # WSD-E4-T2 kill switch (4 escopos) + reassign
    enforcement.py                               # ponto único de enforcement (policy+budget+kill+reassign)
    ledger.py                                    # WSD-E3-T4 ledger de custo DURÁVEL (Postgres)
    control_api.py                               # FastAPI de operador (kill switch/reassign/budget/policy)
    eval_suite/
      __init__.py                                # WSD-E5-T1 suite de eval Tier-2 (dono: WS-D)
      cases.yaml                                 # prompts de referência + asserções
      runner.py                                  # harness (run_suite / run_case)
      __main__.py                                # CLI (gate de promoção de modelo)
  scripts/
    smoke_test.py                                # upgrade simulado
    smoke_baseline.json                           # baseline gravada nesta sessão
    cost_export_cli.py
  tests/
    conftest.py
    test_echo_provider.py                         # Fase 1
    test_gateway_e2e.py                           # Fase 1
    test_virtual_keys_table.py                    # Fase 1
    test_audit_emission.py                        # Fase 1
    test_telemetry.py                             # Fase 1
    test_cost_export.py                           # Fase 1
    test_conformance_gateway_only.py              # Fase 1
    test_policy_enforcement.py                    # Fase 2 WSD-E2-T1
    test_budget_enforcement.py                    # Fase 2 WSD-E2-T2
    test_kill_switch_reassign.py                  # Fase 2 WSD-E4-T2
    test_ledger_durable.py                        # Fase 2 WSD-E3-T4
    test_eval_suite.py                            # Fase 2 WSD-E5-T1
```
