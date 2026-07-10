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
6. **Policy/budget enforcement no call time** (WSD-E2, Fase 2) —
   explicitamente fora de escopo desta Fase 1, não implementado aqui de
   propósito.
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

```
20 passed in 2.05s
```

Cobertura: `echo_provider` isolado (determinismo, shape OpenAI, 404),
round-trip completo mint→call→revoke→denied contra o LiteLLM real,
model-scoping de virtual key (403), tabela `virtual_keys` (insert/revoke
real no Postgres), audit ledger (linhas reais gravadas/consultadas),
telemetria OTel (atributos do contrato + status ERROR em falha), agregação
de custo (multi-tenant, multi-task-class), e conformidade
gateway-only (estática + dinâmica).

## Version pinning e upgrade simulado (WSD-E1-T1)

Ver comentário no topo de `docker-compose.wsd.yml` e docstring de
`scripts/smoke_test.py`. Resumo: imagem sempre por digest, nunca por tag
flutuante; upgrade = trocar o digest + `docker compose ... up -d
--force-recreate model-gateway` + `pytest -q` + `scripts/smoke_test.py`
batendo limpo contra a baseline em `scripts/smoke_baseline.json` antes de
promover.

## Arquivos

```
migrations/0005_wsd.sql                        # tabela virtual_keys (raiz do repo)
docker-compose.wsd.yml                          # serviços model-gateway + model-gateway-echo (raiz do repo)
services/model-gateway/
  litellm_config.yaml                           # config do LiteLLM (bedrock placeholders + eco real)
  pyproject.toml                                # pacote instalável model-gateway-client
  README.md
  echo_provider/
    server.py                                   # "modelo eco" — stdlib puro, determinístico
    Dockerfile
  model_gateway_client/
    __init__.py                                 # superfície pública estável
    settings.py                                  # env vars + leitura real do Vault dev
    db.py                                        # conexão Postgres (virtual_keys)
    virtual_keys.py                              # mint_virtual_key / revoke_virtual_key
    gateway_call.py                              # chat_completion (único caminho de chamada de modelo)
    telemetry.py                                 # spans OTel (contrato dse_contracts.constants)
    cost_export.py                               # agregação de custo por tenant/task_class/stage
    export_api.py                                # FastAPI fino sobre cost_export
    errors.py
  scripts/
    smoke_test.py                                # upgrade simulado
    smoke_baseline.json                           # baseline gravada nesta sessão
    cost_export_cli.py
  tests/
    conftest.py
    test_echo_provider.py
    test_gateway_e2e.py
    test_virtual_keys_table.py
    test_audit_emission.py
    test_telemetry.py
    test_cost_export.py
    test_conformance_gateway_only.py
```
