# WS-A — Ingestão e adaptadores (Fintex DSE, Fase 1)

Este README documenta o workstream inteiro (WS-A): `services/ingest-gateway/`
(este diretório, o núcleo), `services/adapter-slack/` e
`services/adapter-github/`. Os dois adapters importam `ingest_gateway` como
biblioteca — toda a lógica de admissão, correlação, defesas de intake e
steering allowlist vive aqui e é compartilhada, não duplicada.

## O que está implementado e funcionando

### WSA-E1-T3 — Gateway transacional + dispatcher outbox
- `ingest_gateway.gateway.admit_work_item(event, ...)`: grava `work_items` +
  `ingest_events` na MESMA transação Postgres. `work_item_id` e
  `idempotency_key` são derivados deterministicamente de
  `ConversationEvent.event_id` (sha256) — reentregas do mesmo webhook
  convergem via `ON CONFLICT ... DO NOTHING`, nunca duplicando.
- `ingest_gateway.gateway.record_signal_event(...)`: grava um evento de
  sinal (Path B) no mesmo outbox, sem criar `work_items` novo.
- Kill switch por `(tenant_id, channel)` (`channel_kill_switches`,
  `migrations/0002_wsa.sql`) checado ANTES de qualquer INSERT — canal
  desligado não cria WorkItem nem processa, e gera
  `dse_audit.emit(action="admission_blocked_kill_switch")`. Também respeita
  o kill switch tenant-wide do WS-F (`tenant_config.kill_switch_enabled`,
  best-effort/import defensivo — funciona mesmo se essa tabela não existir
  no ambiente).
- `ingest_gateway.dispatcher.Dispatcher`: drena `ingest_events` não
  processados com `SELECT ... FOR UPDATE SKIP LOCKED`. Para
  `kind == "task_request"` chama `Temporal.start_workflow(WORKFLOW_TYPE,
  work_item_id, id=work_item_id, task_queue=TASK_QUEUE)`; para os demais
  kinds, `WorkflowHandle.signal(SIGNAL_NAME, payload)` no workflow já em
  andamento. `WorkflowAlreadyStartedError` é tratada como sucesso
  idempotente (nunca re-lançada). `processed=true` só é marcado depois da
  confirmação Temporal (ou da exceção de duplicado).
  - **Teste central** (`tests/test_dispatcher.py::test_two_concurrent_dispatchers_drain_without_duplication_or_loss`):
    20 ingest_events distintos, 2 dispatchers concorrentes (threads
    separadas, cada uma com seu próprio `Client` Temporal e conexão
    Postgres) drenando a MESMA fila — prova, contra o Temporal e Postgres
    **reais** da infra (não mockados), que não há duplicação nem perda.
    Este é o núcleo do chaos test de saída da Fase 1 (NFR-01) do lado do
    intake.

### WSA-E2-T1 — Verificação de assinatura
- `ingest_gateway.security.verify_slack_signature`: HMAC-SHA256 do signing
  secret sobre `v0:{timestamp}:{body}`, com janela de replay de 5 minutos.
- `ingest_gateway.security.verify_github_signature`: HMAC-SHA256 do webhook
  secret sobre o corpo bruto (`X-Hub-Signature-256`).
- Ambos os adapters rejeitam com 401 + `dse_audit.emit(action=
  "signature_rejected")` qualquer evento não verificável — corpus de
  forgery testado (sem assinatura, assinatura errada, timestamp expirado,
  replay de assinatura antiga válida, corpo alterado após assinado): 100%
  rejeitado (`tests/test_security.py` + `test_signature_pipeline.py` em
  cada adapter).
- Segredos lidos via `dse_secrets` (WS-F, `services/platform/`,
  WSF-E2-T3a) **que já existe nesta sessão** — import real, com fallback
  automático para env var local (`SLACK_SIGNING_SECRET`/
  `GITHUB_WEBHOOK_SECRET`) quando o Vault não tem a versão gravada (nenhum
  Slack App/GitHub App real foi registrado nesta sessão).

### WSA-E2-T2 — Snapshot TOCTOU
- `content_snapshot` é lido diretamente do corpo do webhook recebido —
  nenhum adapter jamais chama `conversations.history`/
  `conversations.replies` (Slack) ou `GET /repos/.../issues/{n}` (GitHub)
  depois. Provado em teste (`test_toctou_snapshot_freezes_content_at_event_time`
  no adapter-slack, `test_toctou_snapshot_not_refetched_on_redelivery_with_edited_body`
  no adapter-github): reenviar o "mesmo" webhook com texto editado é
  deduplicado por `event_id` — o snapshot já persistido nunca é
  sobrescrito.

### WSA-E2-T3 — Sanitização de conteúdo inbound
- `ingest_gateway.sanitize.sanitize_content`: remove Unicode
  invisível/controle (zero-width space/joiner, bidi override — categorias
  Unicode `Cf`/`Cc`) e redige padrões óbvios de token/secret (`ghp_`,
  `xox[bpears]-`, AWS access key id, bloco de chave privada PEM, bearer
  token genérico).
- **Documentado explicitamente como MITIGAÇÃO, não CONTENÇÃO** (ver
  docstring de `ingest_gateway/sanitize.py`): a contenção real que impede
  exfiltração mesmo se um modelo for enganado é o egress proxy
  default-deny do WS-C (`services/egress-proxy/`).
- `content_snapshot` original (o congelado pela defesa TOCTOU) nunca é
  sobrescrito — a versão sanitizada é anexada separadamente como
  `sanitized_content` no `payload` de `ingest_events` (é essa versão que
  deve seguir para qualquer estágio que envolva um modelo).

### WSA-E3 — Adapter Slack (`services/adapter-slack/`)
- **Inbound** (`adapter_slack/app.py`, `POST /slack/events` e
  `POST /slack/interactions`): `app_mention` cria `task_request`; mensagem
  comum numa thread existente correlaciona via `thread_ts`
  (`clarification_answer`); clique de botão (`block_actions`) vira
  `kind=approval`. Tudo passa pelas 4 defesas antes de `correlate()`
  decidir Path A/B. Adapter 100% stateless.
- **Outbound** (`POST /internal/status-comment`): usa
  `dse_contracts.mutable_comment.MutableCommentWriter` com
  `SlackCommentBackend` (real, `slack_sdk.WebClient`,
  `chat.postMessage`/`chat.update`) — exatamente 1 mensagem de status por
  tarefa, editada in-place, nunca uma nova por update. `comment_ref`
  persistido em Postgres (`comment_state`), não em memória — sobrevive a
  reinício do processo.
  - Sem credencial real de Slack App: `FakeSlackClient` in-memory
    documentado substitui o transporte; a lógica (`SlackCommentBackend`,
    `MutableCommentWriter`) é 100% real e é exatamente o que rodaria contra
    a API de verdade.

### WSA-E4 — Adapter GitHub (`services/adapter-github/`)
- **Inbound** (`adapter_github/app.py`, `POST /github/webhook`): issue
  `assigned`/`labeled` (com a label configurável, default `dse`) cria
  `task_request`; comentário de issue comum com `@<bot_login>` cria
  `task_request`; comentário SEM menção numa issue sem WorkItem ativo é
  ignorado (`path: ignored_no_mention`, zero I/O de escrita).
  **Comentário em PR** (via `issue_comment` numa issue que é PR, ou via
  `pull_request_review_comment`) **NUNCA cria WorkItem novo** — só
  correlaciona a um WorkItem ativo por número de PR/issue
  (`kind=review_comment`); sem match, é ignorado com audit
  (`review_comment_ignored_no_active_work_item`). Testado explicitamente
  (`test_pr_issue_comment_never_creates_work_item_even_without_match`).
- **Outbound** (`POST /internal/status-comment`): mesma
  `MutableCommentWriter`, backend `GithubCommentBackend` (real,
  `POST`/`PATCH /repos/{repo}/issues/{issue}/comments` via `requests`),
  autenticado como **GitHub App** (`adapter_github.auth`: JWT RS256 +
  troca por installation access token) — nunca token pessoal.
  - Sem GitHub App real registrada: `FakeGithubClient` in-memory
    documentado substitui o transporte; a lógica de autenticação App
    (`generate_app_jwt`/`get_installation_access_token`) é real (PyJWT +
    `requests` contra `api.github.com`), só faltam
    `GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY`/`GITHUB_APP_INSTALLATION_ID`
    reais.

### WSA-E6-T1 — Correlação Path A/B
- `ingest_gateway.correlate.correlate(conn, tenant_id=, event=,
  requester_principal=)` → `CorrelationResult(kind, work_item_id,
  provenance_work_item_id)`. Lookup determinístico por `source_ref`
  (`{channel,thread_ts}` para Slack, `{repo,number}` para GitHub — o mesmo
  número serve para issue e PR) contra `work_items` com status
  não-terminal.
  - Sem match → `"new_task"` (Path A).
  - Match em item ativo → `"signal"` (Path B) — quem chama decide
    `signal_workflow` (o adapter, neste workstream; ou o WS-B via Temporal
    client).
  - Match em item **terminal** (`done`/`failed`) → `"new_task"` +
    `provenance_work_item_id` preenchido (o caller grava o link de
    proveniência no audit da nova admissão).

### WSA-E6-T2a — Steering allowlist fallback
- `ingest_gateway.steering.is_authorized_to_steer(tenant_id, principal_id)
  -> bool`: fallback explícito de allowlist por tenant
  (`tenant_steering_allowlist`, `migrations/0002_wsa.sql`). Ausência de
  linha = **não autorizado** — nunca "qualquer um pode steerar".
  Assinatura estável (`(tenant_id, principal_id) -> bool`, sem `conn` no
  contrato público) para o WS-F trocar a implementação por um identity-map
  real na Fase 4 sem quebrar `correlate()`/os adapters.
- `correlate()` aplica este gate para `kind in {steering, review_comment}`
  (as duas formas de "alguém injeta direção nova numa tarefa ativa" via
  comentário) — `clarification_answer`/`approval` são respostas esperadas
  do próprio fluxo e não passam pelo gate. Rejeição gera
  `dse_audit.emit(action="steering_rejected_unauthorized")` e retorna
  `"unauthorized"` em vez de `"signal"`.

## O que é fixture/mock local (documentado, não é produção)

- `FakeSlackClient` (`adapter_slack/backend.py`) e `FakeGithubClient`
  (`adapter_github/backend.py`): in-memory, usados nos testes no lugar do
  transporte HTTP real. A lógica de negócio ao redor deles
  (`MutableCommentWriter`, `SlackCommentBackend`/`GithubCommentBackend`,
  `PgCommentStateStore`) é 100% real.
- Segredos (`SLACK_SIGNING_SECRET`, `SLACK_BOT_TOKEN`,
  `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_*`): lidos de env var como fallback
  quando o Vault (via `dse_secrets`) não tem o path gravado — nenhum app
  Slack/GitHub real foi registrado nesta sessão de desenvolvimento.
- `DSE_TENANT_ID` (default `tenant_dev`): Fase 1 é single-tenant de
  desenvolvimento — ver "O que precisa de decisão do arquiteto" abaixo.

## O que precisa de credencial/infra real para produção

1. **Slack App real**: registrar o app, configurar Events API
   (`app_mention`, `message.channels`) e Interactivity apontando para
   `/slack/events`/`/slack/interactions`, gravar `bot_token`/
   `signing_secret` no Vault em `dse/slack/webhook` (chaves `bot_token`,
   `signing_secret`) via `dse_secrets.put_secret`.
2. **GitHub App real**: registrar a App, gerar a chave privada RSA,
   instalar no(s) repo(s) do tenant, gravar `app_id`/`private_key`/
   `installation_id`/`webhook_secret` no Vault em `dse/github/app`.
3. **Vault de produção**: hoje aponta para o Vault dev
   (`localhost:8200`, root token `dse_dev_root`) — produção precisa de um
   Vault real com política de acesso por serviço (não root token).
4. **Multi-tenant real**: `ConversationEvent` não carrega `tenant_id` (é
   puramente um conceito de plataforma) — o mapeamento
   workspace-Slack/org-GitHub → tenant é hoje um único
   `DSE_TENANT_ID` fixo por processo. Produção precisa de uma tabela de
   mapeamento (workspace/org → tenant_id), escopo natural de WS-F/Fase 2
   (identity map completo, ADR-22).

## Pedido ao arquiteto / decisão pendente

- **`SIGNAL_NAME`** (`ingest_gateway/dispatcher.py`, hoje
  `"conversation_signal"`) não está em `dse_contracts.constants` — só
  `TASK_QUEUE`/`WORKFLOW_TYPE` existem lá. Não editei `packages/contracts`
  (fora do meu escopo). Pedido: promover esta constante para
  `dse_contracts.constants.SIGNAL_NAME` assim que o workflow do WS-B
  registrar o signal handler real, para os dois lados importarem do mesmo
  lugar em vez de duplicar a string.
- **Desambiguação clarification vs. steering** em resposta de thread
  comum (Slack) ou comentário de issue comum (GitHub): Fase 1 assume
  default `clarification_answer` porque o adapter não sabe se o bot está
  "aguardando resposta" — esse estado vive no workflow do WS-B. Se o WS-B
  quiser expor esse estado (ex.: via uma coluna em `work_items` ou um
  campo no `plan`), o adapter pode consumir para desambiguar melhor.
- **Colisão de `conftest.py`/pacote `tests` entre serviços**: rodar
  `pytest -q packages services` (o alvo `make test` da raiz) hoje falha
  com `ValueError: Plugin already registered under a different name`
  porque múltiplos serviços (não só os meus) têm um diretório `tests/`
  com `__init__.py` + `conftest.py` do mesmo nome relativo. Cada serviço
  individualmente roda limpo (`cd services/X && pytest -q` — é o fluxo
  documentado no próprio `CONVENTIONS.md`). Uma correção monorepo-wide
  (ex.: `[tool.pytest.ini_options] addopts = "--import-mode=importlib"`
  num `pyproject.toml`/`pytest.ini` na raiz, ou nomes de pacote de teste
  únicos por serviço) é decisão da fundação — não editei `Makefile`/raiz
  por estar fora do meu escopo de diretórios.

## Como rodar os testes

Cada serviço tem seu próprio `pyproject.toml` e roda isolado (evita a
colisão de `conftest.py` descrita acima):

```bash
source /Users/saraiva/Documents/DSE/fase1/.venv-wsa/bin/activate

cd /Users/saraiva/Documents/DSE/fase1/services/ingest-gateway && pytest -q
cd /Users/saraiva/Documents/DSE/fase1/services/adapter-slack && pytest -q
cd /Users/saraiva/Documents/DSE/fase1/services/adapter-github && pytest -q
```

Requer a infra real da fundação no ar (Postgres `localhost:5432`,
Temporal `localhost:7233`) — os testes de `admit_work_item`, `correlate`,
`is_authorized_to_steer` e, principalmente, do `Dispatcher` rodam contra
Postgres e Temporal **reais**, nunca mocks (CONVENTIONS.md: mockar
durabilidade/idempotência anularia o próprio ponto do teste).

### Resultado real desta sessão

```
services/ingest-gateway  : 37 passed
services/adapter-slack   : 14 passed
services/adapter-github  : 19 passed
TOTAL                    : 70 passed, 0 failed
```

Os 3 `Dockerfile` (`services/{ingest-gateway,adapter-slack,adapter-github}/Dockerfile`)
foram testados com `docker build` real nesta sessão (build a partir da
raiz do monorepo) e todos completam com sucesso.

## Nota operacional: Temporal caiu durante esta sessão

Durante o desenvolvimento, o container `dse_temporal` (fundação) estava
`Exited` por um bug de imagem (`DYNAMIC_CONFIG_FILE_PATH` aponta para
`config/dynamicconfig/development-sql.yaml`, que não existe na imagem
`temporalio/auto-setup:1.24` usada pelo `docker-compose.yml`). Sem editar
`docker-compose.yml` (fora do meu escopo), corrigi copiando um arquivo de
dynamic config mínimo vazio para dentro do container parado
(`docker cp` + `docker start dse_temporal`) — não usei `make up`/`down`
nem `docker compose down`, só reiniciei o container já existente. Isso
afeta TODOS os workstreams que dependem de Temporal (WS-B em especial) —
vale o arquiteto adicionar esse arquivo (mesmo vazio) ao repo/imagem para
o próximo `docker compose up` não recriar o container sem ele.
