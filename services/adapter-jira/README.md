# adapter-jira (WS-A, Fase 2 — WSA-E5)

Terceira superfície de intake do DSE (§10.1, UC2/UC5), espelhando a estrutura
do `adapter-github`. Toda a lógica compartilhada (admissão, correlação, 4
defesas de intake, tenant binding) vem de `ingest_gateway` — o adapter é 100%
stateless. A documentação geral do WS-A está em
[`../ingest-gateway/README.md`](../ingest-gateway/README.md); este arquivo
cobre só o que é específico do Jira.

## O que faz

- **Inbound webhook** (`POST /jira/webhook`, `adapter_jira/app.py`):
  - `jira:issue_created` / `jira:issue_updated` com a trigger label (`dse`) ->
    `task_request`.
  - Transição de status para a coluna de aprovação configurada
    (`JIRA_PLAN_APPROVED_STATUS`, ex. "Plano aprovado") -> `kind=approval`
    com `approval_verdict=approved` (UC5 na superfície Jira). Coluna de
    rejeição (`JIRA_PLAN_REJECTED_STATUS`) -> `approval_verdict=rejected` +
    `approval_route=re_plan`. O dispatcher (WSA-E6-T3) roteia isso para
    `SIGNAL_PLAN_APPROVAL` quando o WorkItem está em `awaiting_plan_approval`.
  - `comment_created` -> `clarification_answer`, correlacionado por ticket key.
  - Correlação por **ticket key** (`source_ref = {"ticket_key": "DSE-123"}`).
  - As 4 defesas: assinatura (`X-Hub-Signature` HMAC-SHA256,
    `ingest_gateway.verify_jira_signature`), snapshot TOCTOU (conteúdo lido do
    payload, nunca re-buscado), sanitização (`sanitize_content`), idempotência
    (`event_id` determinístico).

- **Poller de fallback OBRIGATÓRIO** (`adapter_jira/poller.py`,
  `python -m adapter_jira.poller_main`): o webhook Jira é best-effort. O poller
  varre os projetos configurados e reconcilia cada issue pela **mesma via
  idempotente** do webhook (`adapter_jira/ingest.py`). Como os `event_id` são
  derivados do **estado** do issue (id do issue + status + id do comentário —
  nunca do changelog do webhook), webhook e poller convergem no mesmo
  `event_id` e a segunda via a chegar deduplica. **Nunca duplicam** (provado em
  `tests/test_poller_webhook_idempotency.py`, nos dois sentidos).

- **Outbound**:
  - **Transições serializadas por ticket** (`adapter_jira/transitions.py`,
    `POST /internal/transition` enfileira; `python -m
    adapter_jira.transition_main` drena). Jira Cloud rejeita transições
    concorrentes no mesmo issue; o worker garante, via advisory lock por
    ticket (`pg_try_advisory_lock(hashtext(ticket_key))`), que só uma transição
    por ticket roda de cada vez — tickets diferentes seguem em paralelo.
    Enfileiramento idempotente por `dedup_key`.
  - **Status comment único** (`POST /internal/status-comment`): a MESMA
    `dse_contracts.mutable_comment.MutableCommentWriter` dos adapters
    Slack/GitHub, com um `JiraCommentBackend` novo (surface `jira`, mesma
    tabela `comment_state`).

## Rodando localmente

```bash
source /Users/saraiva/Documents/DSE/fase1/.venv-wsa/bin/activate
pip install -e ../../packages/contracts -e ../../packages/dse_audit -e ../../packages/dse_identity \
            -e ../../services/platform -e ../../services/ingest-gateway -e .
JIRA_WEBHOOK_SECRET=dev_only_fixture JIRA_TRIGGER_LABEL=dse \
  DSE_DATABASE_URL=postgresql://dse_app:dse_app_dev_only@localhost:5432/dse \
  uvicorn adapter_jira.app:app --port 8804
```

Endpoints: `POST /jira/webhook`, `POST /internal/status-comment`,
`POST /internal/transition`, `GET /health`.

## Testes

```bash
cd /Users/saraiva/Documents/DSE/fase1/services/adapter-jira && pytest -q
```

Resultado desta sessão: **17 passed**. Requer Postgres real (`localhost:5432`,
migrações `0002_wsa.sql` + `0008_wsa2.sql` aplicadas) — sem mocks de DB. Jira em
si é 100% fixture (`FakeJiraClient`); a lógica de negócio (backend, serialização
de transição, poller, ingestão) é a real.

## O que é fixture/mock local (não é produção)

- `FakeJiraClient` (`adapter_jira/backend.py`): in-memory, substitui o
  transporte HTTP nos testes. `RealJiraClient` (REST API v3 do Jira Cloud,
  `requests` + Basic auth com service account) é o que roda em produção.
- Segredos (`JIRA_WEBHOOK_SECRET`, `JIRA_BASE_URL`, `JIRA_ACCOUNT_EMAIL`,
  `JIRA_API_TOKEN`): lidos do Vault (`dse/jira/service_account`) via
  `dse_secrets`, com fallback para env var — nenhum site Jira real foi
  registrado nesta sessão.

## Gaps / o que precisa de infra real (documentado, não escondido)

1. **Site Jira Cloud real**: registrar a service account com token escopado
   (project-level), criar o webhook dinâmico com secret (para o
   `X-Hub-Signature`), e gravar `base_url`/`email`/`api_token`/`webhook_secret`
   no Vault em `dse/jira/service_account`. Sem isso, `RealJiraClient` não é
   exercitado ponta a ponta.
2. **Atribuição de aprovação pelo poller**: o poller vê só o estado atual do
   issue, não o changelog, então NÃO sabe QUEM fez uma transição. Uma aprovação
   reconstruída pelo poller (webhook descartado) é atribuída ao principal de
   sistema `system:adapter-jira-poller`; o webhook, quando não é descartado,
   carrega o ator real e prevalece por chegar primeiro (dedup por `event_id`).
   Tarefas não têm essa limitação (atribuídas ao reporter, estável).
3. **Ordem de transições entre workers**: o advisory lock garante a invariante
   dura do Jira (nunca concorrente no mesmo ticket). A ordem estrita de
   transições enfileiradas quase-simultaneamente é preservada dentro de um
   worker (por `id`); entre workers distintos é best-effort — em produção roda
   um único worker de transição por padrão.
4. **`SIGNAL_PLAN_APPROVAL` handler**: o dispatcher roteia para o nome de signal
   correto (`dse_contracts.SIGNAL_PLAN_APPROVAL`); o `@workflow.signal`
   correspondente é construído por WS-B (WSB-E3-T2) em paralelo. Até existir, um
   approval roteado é entregue ao Temporal e (sem handler) descartado — sem erro.
