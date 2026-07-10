# services/orchestrator — WS-B: Orquestração Temporal

Worker Temporal (`temporalio` Python SDK) e a máquina de estados
`WorkItemLifecycleWorkflow` que dirige o ciclo de vida completo de um
WorkItem na Fase 1 do Fintex DSE: intake → gate de clarificação → Coder
único → L1 → PR → CI → review humano → merge humano → Done, com
blocked/failed/escalated como estados terminais e nenhuma decisão de fluxo
tomada por LLM (P1) ou por uma sessão de agente sobre o próprio trabalho
(P3).

## Status — o que está implementado e funcionando

Todas as tarefas P0 do enunciado (WSB-E1, E2, E3-T1/T4, E5) estão
implementadas e cobertas por teste automatizado rodando contra o Postgres e
o Temporal **reais** da infra da fundação (nunca mockados):

- **WSB-E1-T1/T2/T4** — `worker.py`: conecta em `localhost:7233` (ou
  `DSE_TEMPORAL_ADDRESS`), task queue = `dse_contracts.constants.TASK_QUEUE`,
  build id fixo (`--build-id`/`DSE_WORKER_BUILD_ID`), health endpoint HTTP em
  `:8900` (`GET /health`), interceptor OpenTelemetry real
  (`temporalio.contrib.opentelemetry.TracingInterceptor`, configurado em
  `otel_interceptor.py`) e a Activity `emit_audit_event`
  (`dse_contracts.activities.ACTIVITY_EMIT_AUDIT`) que grava no audit ledger
  via `dse_audit.emit` a partir de dentro de uma Activity. Runbook de
  Worker Versioning / drain-and-cutover em `RUNBOOK.md`.
- **WSB-E2** (todas as 4 tarefas) — `workflows.py`:
  `WorkItemLifecycleWorkflow` (`@workflow.defn(name=WORKFLOW_TYPE)`)
  implementa a máquina de estados completa usando
  `dse_contracts.work_item.WorkItemStatus`; toda I/O vive em Activity (nunca
  direto no corpo do workflow — ver seção "Disciplina de determinismo"
  abaixo); `continue_as_new` fecha a fase de intake; `start_workflow` é
  idempotente por `workflow_id=work_item_id` (o Temporal rejeita
  `WorkflowAlreadyStartedError` nativamente — não precisei implementar
  nada extra); Activities cross-workstream sequenciadas por NOME
  (`workflow.execute_activity(ACTIVITY_..., ...)`); sinais de steering
  (`clarification_answer`, `review_comment`, `approval_or_rejection` — este
  último implementado como o par `review_comment`+`merged_by_human`, ver
  nota abaixo) correlacionados externamente pelo WS-A.
- **WSB-E3-T1** — gate de clarificação: checklist determinístico via
  Activity (`check_clarification_completeness` — repo/base_branch/
  acceptance_criteria), timer de reminder configurável + escalação,
  rounds capados (default 3), nunca "adivinha" — esgotado o cap ou a janela
  de reminder+escalação sem resposta, transiciona a `escalated`.
- **WSB-E3-T4** — loop de review humano: espera durável por
  `review_comment` (`changes_requested`/`approved`); `changes_requested`
  volta ao Coder no MESMO branch/PR, re-valida L1, re-finaliza o MESMO PR;
  `approved` espera um SEGUNDO sinal (`merged_by_human`) e só então
  transiciona a Done. **Não há nenhuma chamada à API de merge do GitHub em
  lugar nenhum do código** — provado estaticamente em
  `tests/test_review_loop.py::test_no_automatic_merge_path_in_source`
  (grep por padrões de chamada real, não por prosa em comentário).
- **WSB-E5** (as 3 tarefas):
  - T1: checkpoint ao fim de cada fase (`ACTIVITY_CHECKPOINT_SANDBOX`) com
    retries limitados; esgotado, tenta rebuild (`ACTIVITY_REBUILD_SANDBOX`);
    esgotado também, escala.
  - T2: signals/queries de operador — `pause`/`resume`, `cancel` (+teardown
    via Activity), `retry_from_checkpoint`, `force_clarification`,
    `escalate`, `reassign_model`, `reassign_runtime`; kill switch checado
    antes de CADA Activity de negócio (nunca mata uma Activity em
    andamento); toda ação de operador que causa uma transição chama
    `ACTIVITY_EMIT_AUDIT`.
  - **T3 (crítico) — suíte de chaos** (`tests/test_chaos.py`): mata de
    verdade o **processo** de um worker Temporal (SIGKILL) no meio de uma
    Activity longa, sobe um segundo worker, e prova via consulta ao
    Postgres/audit ledger reais que o workflow retomou sem perder progresso
    e sem duplicar efeitos de negócio (`pr_finalized`, `merged_by_human`,
    `sandbox_provisioned` aparecem exatamente 1x cada, apesar de a Activity
    de baixo nível ter sido reexecutada — comportamento correto de
    at-least-once).

### Nota sobre nomes de sinal

O enunciado lista `approval_or_rejection` como um dos sinais de steering.
Implementei o veredito de review como `review_comment` (payload com
`verdict: "changes_requested" | "approved"`) seguido, no caso aprovado, de
um segundo sinal dedicado `merged_by_human` — porque "aprovado" e "mergeado"
são dois eventos de fato distintos e correlacionados a webhooks diferentes
do GitHub (`pull_request_review` vs. `pull_request.closed+merged=true`), e
juntar os dois num só sinal esconderia exatamente o ponto de P3 (aprovação
≠ merge). `clarification_answer` está implementado literalmente como
pedido.

## O que está com fixture/fake local (por design desta tarefa)

As Activities cross-workstream de WS-C (`sandbox_runtime.activities`) e
WS-E (`validation.activities`) **ainda não existem** (constrói-se em
paralelo). Todos os testes usam **Activities FAKE** que implementam a MESMA
assinatura/nome/tipo de retorno de `dse_contracts.activities`
(`tests/fakes.py`) — nunca mockamos Postgres nem Temporal em si, só essas
duas fronteiras que pertencem a outro workstream. `worker.py` tenta
importar os módulos reais defensivamente (`try/except ImportError`, ver
seção seguinte) e cai de volta a rodar só com as Activities locais do WS-B
se eles ainda não existirem.

## Contrato de Activity assumido para as fronteiras de WS-C/WS-E

`dse_contracts.activities` define **nomes** e **tipos de retorno**, mas não
o schema do payload de entrada de cada Activity (isso é deliberadamente
deixado em aberto para paralelizar o desenvolvimento). Assumi o seguinte
contrato de payload (um único `dict` por chamada) — **a reconciliar na
integração** com quem implementar de verdade:

| Activity | payload assumido (dict) | retorno |
|---|---|---|
| `provision_sandbox` | `work_item_id, tenant_id, repo, base_branch` | `SandboxHandle` |
| `run_coder_turn` | `sandbox_id, work_item_id, tenant_id, instructions: list[str], model_override, runtime_override` | `CoderTurnResult` |
| `checkpoint_sandbox` | `sandbox_id, work_item_id, phase` | `CheckpointRef` |
| `rebuild_sandbox` | `work_item_id, sandbox_id` | `SandboxHandle` |
| `teardown_sandbox` | `sandbox_id, work_item_id, reason` | `None` |
| `run_l1_pipeline` | `work_item_id, sandbox_id` | `L1Result` |
| `finalize_pr` | `work_item_id, tenant_id, sandbox_id, repo, base_branch, branch, existing_pr_number?` | `PrRef` |
| `post_tracking_comment` | `work_item_id, tenant_id, pr_number, status` | `None` |
| `consume_ci_status` | `work_item_id, pr_number` | `CiStatusResult` |

Quando WS-C/WS-E terminarem, registrem em `services/sandbox-runtime/activities.py`
e `services/validation/activities.py` uma lista `ACTIVITIES` (lista de
callables decorados com `@activity.defn(name=...)`) — `worker.py` importa
esses dois módulos automaticamente (`_load_cross_workstream_activities`) e
registra tudo que encontrar, sem precisar editar `worker.py` de novo. Se o
payload real divergir do assumido acima, é só ajustar os `dict`s montados em
`workflows.py` (todos centralizados, fácil de achar via grep por
`ACTIVITY_`).

## Achado real de integração: contrato de `start_workflow`

Ao rodar o worker contra o Temporal real da infra compartilhada (o mesmo
`dse-core-task-queue` que o WS-A também usa), descobri que o dispatcher do
WS-A parece chamar `StartWorkflow` passando **apenas o `work_item_id`
(string)** como argumento, não um `WorkItemLifecycleInput` completo. Para
não travar a integração nisso, `WorkItemLifecycleWorkflow.run` agora aceita
`Any` e faz uma coerção defensiva (`_coerce_input`, em `workflows.py`):

- `WorkItemLifecycleInput` → usa direto (é sempre isto que os
  `continue_as_new` internos passam).
- `dict` → constrói `WorkItemLifecycleInput(**dict)`.
- `str` → trata como `work_item_id` e busca o resto (`tenant_id`, `repo`,
  `base_branch`, `requester`, `pr_number`) na tabela `work_items` via uma
  nova Activity local, `load_work_item`.

Isso significa que o WS-A pode continuar chamando
`start_workflow(workflow_id=work_item_id, args=[work_item_id])` (ou com o
objeto completo, se preferirem alinhar) sem quebrar. **A reconciliar**: se
o time do WS-A confirmar qual formato é o definitivo, dá para simplificar
removendo a branch não usada.

## O que precisa de credencial/infra real para produção

- **Coder/L1/PR/CI reais**: dependem das Activities de WS-C/WS-E (sandbox
  Docker, OpenHands, egress proxy, LiteLLM, GitHub App, SAST/secret-scan).
  Nada disso é implementado aqui — só a orquestração que os invoca por
  nome.
- **OpenTelemetry**: por default exporta para o console
  (`DSE_OTEL_EXPORTER=console`, modo local/dev). Para produção, configure
  `DSE_OTEL_EXPORTER=otlp` + `DSE_OTEL_EXPORTER_OTLP_ENDPOINT=<host:porta>`
  de um collector real (WS-F) e instale
  `opentelemetry-exporter-otlp-proto-grpc` (não é dependência obrigatória
  deste pacote — cai de volta ao console com um aviso claro se ausente).
- **Worker Versioning**: desligado por default (`DSE_WORKER_USE_VERSIONING=false`)
  — ver `RUNBOOK.md` para como e quando ativar em produção.

## Disciplina de determinismo (P1) e o "clobber bug" que este código evita

Durante o desenvolvimento, os testes pegaram uma corrida de sinais real:
resetar uma flag de "sinal recebido" **antes** de esperar por ela (um padrão
comum, `self._x_received = False; await wait_condition(...)`) pode apagar
um sinal que já chegou enquanto o workflow ainda estava processando uma
Activity anterior no mesmo laço — porque o *handler* do sinal roda assim
que o event loop do workflow cede controle (a cada `await`), não só quando
chegamos ao `wait_condition`. `workflows.py` documenta isso inline em cada
lugar onde importa (`_run_intake_phase`, `_run_review_phase`) e a regra
adotada é: **nunca resete uma flag de sinal antes de consumi-la; resete
sempre DEPOIS de ler o payload**.

Um segundo achado, documentado em `_run_implementation_phase`: chamar
`continue_as_new` imediatamente antes de uma fase que começa esperando um
sinal externo (ex.: `review_comment`) é uma corrida real — um sinal
endereçado ao run "antigo" que está fechando pode ser perdido, nunca
entregue ao run novo. Por isso `continue_as_new` só é usado na transição
intake→implementação (comprovadamente segura: nada relevante pode chegar
antes de o PR existir); implementação→review e todo o loop de review
rodam na MESMA execução (um `while True`), sacrificando um pouco de reset
de histórico em favor de correção comprovada por teste. Isso está
documentado inline no código (`# NAO fazemos continue_as_new aqui`) e é o
motivo de `test_chaos.py` também servir como regressão para essa classe de
bug (replay do histórico de uma execução mais longa).

## Arquivos

```
services/orchestrator/
  pyproject.toml
  Dockerfile
  RUNBOOK.md
  README.md
  src/dse_orchestrator/
    __init__.py
    config.py            # OrchestratorConfig + from_env() + apply_to_input()
    models.py             # WorkItemLifecycleInput/Result, fases, OperatorEvent
    local_activities.py   # update_work_item_status, check_clarification_completeness,
                           # emit_audit_event (ACTIVITY_EMIT_AUDIT), load_work_item
    otel_interceptor.py   # setup_tracing() -> TracingInterceptor real
    workflows.py           # WorkItemLifecycleWorkflow (a maquina de estados)
    worker.py              # entrypoint: Client.connect + Worker + health endpoint
  tests/
    conftest.py            # helpers de Postgres real + fixture time_skipping_env
    fakes.py                # Activities FAKE (mesma assinatura de dse_contracts.activities)
    test_lifecycle_happy_path.py
    test_clarification_gate.py
    test_review_loop.py
    test_operator_controls.py
    test_chaos.py           # WSB-E5-T3 (chaos real, worker process morto)
    chaos_worker_process.py # subprocesso usado pelo chaos test
../../docker-compose.wsb.yml  # fragment reservado do WS-B (porta 8900)
../../migrations/0003_wsb.sql # NAO CRIADO — ver nota abaixo
```

### Sobre `migrations/0003_wsb.sql`

Não foi criado. O Temporal persiste seu próprio estado de workflow/signal/
timer no Postgres da fundação (schema gerenciado pelo `auto-setup` do
próprio Temporal, fora do nosso controle). A única tabela compartilhada que
o orquestrador escreve é `work_items` (coluna `status`/`pr_number`), já
criada pela migração `0001_foundation.sql` da fundação — não há necessidade
de uma tabela própria do WS-B nesta Fase 1.

## Conexão com o chaos test do WS-A (NFR-01, as duas metades)

`test_chaos.py` mata o **worker** que executa workflows/activities depois
de já iniciados. O chaos test equivalente do WS-A (WSA-E1-T3) mata o
**dispatcher** — o processo que faz `SELECT ... FOR UPDATE SKIP LOCKED` na
tabela `ingest_events`/`work_items` e chama `StartWorkflow`. Juntos, os dois
testes cobrem as duas metades do NFR-01 (durabilidade ponta-a-ponta):

- WSA-E1-T3 prova que nenhum evento fica preso ou é processado duas vezes
  **antes** do workflow existir (a fase de intake/outbox).
  `SELECT...FOR UPDATE SKIP LOCKED` garante que, se o dispatcher morrer
  depois de pegar a lock mas antes de confirmar o `StartWorkflow`, a row
  fica disponível para outro processo pegar (lock liberada no rollback da
  transação); `start_workflow(workflow_id=work_item_id)` sendo idempotente
  garante que, se ele morrer DEPOIS do StartWorkflow mas antes de marcar o
  evento como `processed`, reprocessar o mesmo evento não duplica o
  workflow (Temporal rejeita o `WorkflowAlreadyStartedError`).
- WSB-E5-T3 (este arquivo) prova que nenhum progresso é perdido ou
  duplicado **depois** que o workflow já existe e está em voo, mesmo que o
  worker que o executava morra no meio de uma Activity.

## Como rodar os testes

```bash
cd /Users/saraiva/Documents/DSE/fase1
python3.12 -m venv .venv-wsb
source .venv-wsb/bin/activate
pip install -e packages/contracts -e packages/dse_audit -e packages/dse_identity
pip install -e services/orchestrator
pip install pytest pytest-asyncio

cd services/orchestrator
pytest -q
```

Pré-requisito: a infra da fundação já no ar (`docker compose up -d` já
rodou antes desta sessão — Postgres em `localhost:5432`, Temporal em
`localhost:7233`). Os testes usam:
- `temporalio.testing.WorkflowEnvironment.start_time_skipping()` (um
  servidor Temporal de teste real, efêmero, com aceleração de tempo) para
  a maioria dos testes — acelera os timers de reminder/escalação de
  horas/dias para segundos sem sleep real.
- O Temporal **real** da infra (`localhost:7233`) para `test_chaos.py`
  especificamente — porque matar um processo de worker de verdade só prova
  algo contra um servidor de verdade.
- O Postgres **real** da infra para toda escrita de audit/status
  (`dse_audit.emit`, `update_work_item_status`) em todos os testes.

`_require_postgres` (autouse fixture em `conftest.py`) pula os testes com
uma mensagem clara se o Postgres da fundação não estiver acessível, em vez
de falhar com um erro de conexão críptico.

## Resultado real da suíte (rodado nesta sessão)

```
17 passed in ~23s
```

Cobertura: 3 testes de ciclo de vida feliz + retry de L1 (2 variações),
4 testes de gate de clarificação (completo, reminder, escalação por
silêncio, cap de rounds), 4 testes de loop de review (changes_requested,
CI red, espera de merge explícito, grep estático anti-merge-automático),
4 testes de controles de operador (pause/resume, cancel+teardown, reassign,
escalate), 1 chaos test crítico + 1 sanity check do script auxiliar.

## O que ficou incompleto / limitações conhecidas

1. **Payload de Activity cross-workstream é uma suposição documentada**
   (ver tabela acima) — só será validado de verdade quando WS-C/WS-E
   publicarem suas Activities reais e o worker as importar via
   `_load_cross_workstream_activities`.
2. **`continue_as_new` não é usado em todas as fronteiras de fase** listadas
   no enunciado (só intake→implementação) — decisão deliberada após achar
   uma corrida de sinal real em teste (ver seção "Disciplina de
   determinismo" acima). O histórico de uma execução com muitos ciclos de
   `changes_requested` cresce mais do que o ideal; para o volume esperado
   na Fase 1 (poucos ciclos de review por PR) isso é aceitável, mas fica
   registrado como uma limitação a revisitar se o Temporal reclamar de
   tamanho de histórico em produção (ele avisa via
   `workflow_task_timeout`/warnings de tamanho antes de virar erro).
3. **Race residual em sinais de operador na fronteira intake→implementação**:
   como só essa transição usa `continue_as_new`, um sinal de operador
   (`pause`/`cancel`/etc.) enviado no instante exato dessa transição
   teoricamente pode ser perdido pelo mesmo motivo do item 2. Mitigação:
   o operador pode reenviar o sinal (idempotente do ponto de vista de
   negócio); todas as OUTRAS fronteiras (dentro de implementação e review,
   que agora é a maior parte da vida útil do workflow) não têm esse risco.
4. **Sem fairness/budget enforcement** (WSB-E1-T3 e WSB-E4) — Fase 2, fora
   de escopo conforme o enunciado.
5. **Sem split Planner/Tester/Reviewer nem gate de aprovação de plano por
   risk class** — Fase 2, fora de escopo conforme o enunciado.
6. **`worker.py` roda sem TLS/mTLS para o Temporal** (`Client.connect`
   simples) — adequado para o Temporal dev local; produção precisaria de
   `tls=...`/namespace real, fora do escopo desta sessão local.
