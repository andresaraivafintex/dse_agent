# services/orchestrator — WS-B: Orquestração Temporal

Worker Temporal (`temporalio` Python SDK) e a máquina de estados
`WorkItemLifecycleWorkflow` que dirige o ciclo de vida completo de um
WorkItem na Fase 1 do Fintex DSE: intake → gate de clarificação → Coder
único → L1 → PR → CI → review humano → merge humano → Done, com
blocked/failed/escalated como estados terminais e nenhuma decisão de fluxo
tomada por LLM (P1) ou por uma sessão de agente sobre o próprio trabalho
(P3).

## Fase 4 ("Loop hardening & learning") — o que foi adicionado

A Fase 4 estende (não reescreve) a máquina de estados das Fases 1-3. Três
entregas de WS-B, todas cobertas por teste real contra Postgres/Temporal da
fundação (7 testes novos em `tests/test_phase4_merge_base_and_learning.py`;
suíte total **54 passed**). **Nenhuma migração nova de WS-B** — reuso a tabela
`skill_episode` da migração `0019_wsc4.sql` (dona WS-C) só para INSERT. Só
IMPORTO de `dse_contracts.activities` (`ACTIVITY_UPDATE_BASE_BRANCH` +
`UpdateBaseBranchResult`, já promovidos no gate de entrada) — nenhuma
redefinição local.

### WSE-E6-T16 — merge-base no review loop (wiring WS-B)

No caminho `changes_requested` do review loop, ANTES de re-rodar o Coder, o
workflow chama `ACTIVITY_UPDATE_BASE_BRANCH` (helper
`_update_base_branch_before_review_fix`). Invariantes:

- **`first_human_review_done=True` sempre** neste ponto: chegamos aqui
  justamente porque um humano já revisou o PR e pediu mudanças. Depois do 1º
  review a estratégia só pode ser merge-base-into-branch — um rebase+force-push
  orfanaria as threads de review ancoradas nos commits reescritos
  (comportamento verificado do GitHub, failure mode 11). A escolha da
  estratégia é determinística e vive no WS-E (P1: código, não modelo); o WS-B
  só passa a flag correta e reage ao resultado.
- **`conflict=True` → escala a humano** (`_EscalateNow`), NUNCA resolve à força
  (P6). A escalação carrega repo/branch/base para o humano agir.
- **`orphaned_threads>0` → escala** também: é a asserção de exit da Fase 4
  (zero órfãs). Se o dono (WS-E) reportar >0, a invariante foi violada e o
  workflow não segue adivinhando (P6). O merge-base real garante 0 por
  construção; esta é uma defesa-em-profundidade do lado do chamador.
- O call site é o payload EXATO de `WSB_UPDATE_BASE_PAYLOAD` em
  `packages/contracts/tests/test_activity_boundaries.py` (regra do arquivo:
  call site + boundary test mudam juntos — aqui o payload já existia e bate,
  então nada mudou na fundação). As fakes de teste decodificam com o model REAL
  `UpdateBaseBranchInput` e retornam `UpdateBaseBranchResult`.
- **Escopo deliberado**: só o caminho `changes_requested`. É exatamente onde há
  threads de review para orfanar. No caminho CI-red (antes de qualquer review
  humano) não há threads ancoradas — o drift ali é absorvido pelo próprio ciclo
  de fix, e rebase seria até permitido; por isso o merge-base não é forçado lá.

### WSC-E4-T2 — episódio de clarificação (source do insumo de skill-learning)

O gate de clarificação já detecta a mesma lacuna recorrendo. Quando um campo
que **já faltou num round anterior deste intake** reaparece faltando (detecção
por set-arithmetic pura no workflow — determinística, P1), o workflow grava UM
`skill_episode` (source=`clarification`) na tabela da migração 0019 via a
Activity local `record_skill_episode`:

- `pattern_key = "clarification_missing:<campos+recorrentes>"` agrupa
  ocorrências do mesmo padrão; `occurrence_n` é o contador tenant-wide;
  `provenance` (JSONB) carrega repo/campos/round/requester.
- **NENHUMA skill é criada/ativada aqui** (fronteira testada em
  `packages/contracts` — WS-B não tem activity de promoção). O episódio é só o
  insumo governável que a esteira de promoção do WS-C consome. A primeira lacuna
  (round inicial, antes de qualquer pedido) NUNCA vira episódio — só a
  recorrência conta (`test_non_recurring_clarification_emits_no_episode`).

### Pilot gate "PR quality thresholds" — métricas de qualidade de PR

Emito quatro métricas OTel via o mesmo mecanismo da Fase 3 (`metrics.py` +
Activity local `emit_pr_quality_metric`, leitura determinística no workflow /
emissão fora do sandbox — P1), numa fronteira TERMINAL do PR (merge OU
escalação, com o atributo `dse.pr.outcome`):

| Métrica | Significado |
|---|---|
| `dse.pr.review_rounds` | rounds de review humano/CI-red por PR (`review_round`) |
| `dse.pr.changes_requested_total` | quantos lotes de `changes_requested` o PR acumulou |
| `dse.pr.time_to_merge_seconds` | de PR finalizado (`workflow.now()`, replay-safe) até `merged_by_human` |
| `dse.pr.evidence_refreshes` | refreshes de evidência do PR (proxy de evidence-consumption) |

**Estas quatro alimentam o pilot gate "PR quality thresholds" (adendo 03).** O
`time_to_merge` só é emitido no desfecho `merged`. A **evidence-consumption
autoritativa** (quem acessou qual artefato, quando) é logada pelo WS-E no seu
access log — o WS-B contribui o refresh count como proxy do lado do workflow.
Honestidade do adendo 03 (bloqueio administrativo): **os NÚMEROS reais só saem
operando contra repos/modelos reais** — GitHub App / Slack / conta Bedrock
reais são pré-requisito dos pilot gates e são o item de maior lead time. Aqui a
INSTRUMENTAÇÃO está pronta e testada (emite determinística e corretamente com
os atributos que o gate consulta); falta só a operação real para popular os
histogramas com dados de piloto.

## Fase 3 ("Evidence") — o que foi adicionado

A Fase 3 estende (não reescreve) a máquina de estados das Fases 1+2. Tudo
abaixo é coberto por teste real contra Postgres/Temporal da fundação (10
testes novos; suíte total **47 passed**). Migração nova:
`migrations/0014_wsb3.sql` (tabela `work_item_evidence`). Contratos: só
IMPORTA de `dse_contracts.activities` (nomes + models de evidência já
promovidos à fundação no gate de entrada) — nenhuma redefinição local.

### WSB-E4-T2 — Iteration caps + debounce de refresh de evidência (ADR-26)

- **Nenhum loop do workflow é infinito por construção** — todo `while` tem cap
  testado: clarificação (`clarification_round_cap`, Fase 1), fix L1
  (`coder_retry_cap`, Fase 1), objeções L2 (`l2_retry_cap`, Fase 2), re_plan
  (`plan_round_cap`, Fase 2) e — novo — **rounds de review**
  (`review_round_cap`, default 20; esgotado → `escalated`, testado em
  `test_iteration_caps_debounce.py`). Todos os caps vêm do INPUT do workflow,
  preenchidos por `config.from_env()` (`DSE_REVIEW_ROUND_CAP` etc.) — mudam
  sem redeploy; por-tenant é possível lendo `tenant_config` antes de
  `apply_to_input` no dispatcher.
- **Debounce de evidência (ADR-26), 100% determinístico (P1)**: evidência é
  re-gerada **somente** quando (a) um fix cycle executou (= commit novo que
  muda comportamento) ou (b) um humano pediu explicitamente (novo
  `@workflow.signal refresh_evidence`). Comentários de review **acumulam numa
  lista** e são consumidos em LOTE: se o lote pede mudanças e há janela
  configurada (`evidence_debounce_seconds`, default prod 300s; 0 = sem
  janela), o workflow espera a janela num timer durável para agrupar
  comentários ainda chegando → **6 comentários numa janela = 1 ciclo de fix +
  1 refresh** (provado com time-skipping em
  `test_six_review_comments_in_window_trigger_at_most_one_refresh`).
- **Cap de refreshes** (`evidence_refresh_cap`, default 5, além do inicial):
  excedido → declina LIMPO e auditado (`evidence_refresh_declined_cap`, P6);
  a evidência fica stale mas o PR nunca é bloqueado.

### Wiring do pipeline de evidência (depois de `finalize_pr`)

```
finalize_pr -> trigger_preview(files_changed do CoderTurnResult, FR-20)
   ├─ skipped_backend_only -> registra e segue (conta como sucesso)
   ├─ degraded / Activity caiu -> evidence_degraded (failure mode 9) e SEGUE
   └─ created -> run_demo_evidence(base_url do preview; publish INTERNO, WS-E)
                   -> run_visual_diff (base_screenshot_key=None no 1º run -> baseline)
```

- Nomes/models importados de `dse_contracts` (`ACTIVITY_TRIGGER_PREVIEW`/
  `RUN_DEMO_EVIDENCE`/`RUN_VISUAL_DIFF`, `PreviewRef`/`DemoEvidenceResult`/
  `VisualDiffResult`). **Falha de preview NÃO bloqueia o PR** (failure mode
  9): qualquer degradação vira audit `evidence_degraded` + projeção e o fluxo
  segue para review humano — provado em `test_evidence_pipeline.py`
  (degraded e crash total da Activity).
- Os testes usam fakes **tipadas pelos models REAIS do contrato**: cada fake
  decodifica o payload com `TriggerPreviewInput(**payload)` etc. — payload
  derivado do contrato quebra no teste, não no wire (lição do adendo 02).
  Os payloads exatos dos call sites também estão em
  `packages/contracts/tests/test_activity_boundaries.py` (única edição feita
  em `packages/`, permitida pela regra do arquivo: call site e boundary test
  mudam juntos).
- Projeção durável consultável: tabela `work_item_evidence` (migração 0014,
  upsert idempotente via Activity local `record_evidence_state`) — o queue
  board (WS-F) lê "qual o preview/vídeo mais recente deste PR?" sem varrer o
  audit ledger (que continua a fonte imutável, P8).

### Ativação do alerta de history (com WS-F — ALERTING-RULES.md §3)

- O workflow lê `workflow.info().get_current_history_length()` /
  `get_current_history_size()` (APIs replay-safe do SDK) e a contagem de
  Continue-As-New (`continue_as_new_count`, incrementada em TODA transição
  `_continue_as_new`) e emite via Activity local `emit_history_metric`
  (I/O fora do sandbox — P1) as métricas OTel:
  - `dse.workflow.history_length` (histogram, `{event}`)
  - `dse.workflow.history_size_bytes` (histogram, `By`)
  - `dse.workflow.continue_as_new_count` (histogram, `{run}`)
  com atributos `dse.work_item_id`, `dse.tenant_id`, `dse.stage`,
  `dse.checkpoint`. Emitida: antes de cada `continue_as_new`, após
  `pr_finalized` e a **cada volta do review loop** (exatamente onde o history
  cresce sem Continue-As-New — a limitação documentada da Fase 1 que o
  debounce mitiga). Best-effort: falha de métrica nunca afeta o fluxo.
- Exporter: mesmo env do tracing — `DSE_OTEL_EXPORTER=console` (default) ou
  `otlp` + `DSE_OTEL_EXPORTER_OTLP_ENDPOINT` (o fragment
  `docker-compose.wsb.yml` já aponta `otel-collector:4317` do WS-F). **WS-F
  aponta a regra §3 (Warning 70% / Critical 90% de ~10k eventos) para
  `dse.workflow.history_length`.** Testado com `InMemoryMetricReader` real do
  SDK OTel em `test_history_metric.py`.

### Pedidos de campo ao contrato (documentados, NÃO editei a fundação)

1. **`DemoEvidenceResult` não carrega chave/caminho de screenshot** — o
   gatilho "run_visual_diff quando houver screenshot" hoje é aproximado
   deterministicamente por "a demo produziu mídia (video/trace)" + a convenção
   `demos/<work_item_id>/screenshot.png` (ADR-27). Pedido: campo
   `screenshot_artifact_key: str | None` (ou `screenshot_path`) em
   `DemoEvidenceResult`.
2. **`VisualDiffResult` não retorna a chave da baseline criada** — quando
   `baseline_created=True`, assumo que WS-E devolve a chave da baseline em
   `diff_artifact_key` (guardada em `visual_baseline_key` para o próximo run).
   Pedido: campo `baseline_artifact_key: str | None`.

### Novos signals/campos (Fase 3)

- `@workflow.signal refresh_evidence(payload=None)` — pedido humano explícito
  de refresh (roteável pelo WS-A a partir de um comentário/botão dedicado).
- `WorkItemLifecycleInput`: `review_round_cap`, `evidence_debounce_seconds`,
  `evidence_refresh_cap`, `evidence_refreshes`, `preview_status/url`,
  `evidence_passed/video_key/trace_key`, `visual_baseline_key`,
  `last_files_changed` (refresh humano não tem commit novo — reusa o último
  conjunto para o paths-filter FR-20), `continue_as_new_count`.

## Fase 2 ("Judgment & queue") — o que foi adicionado

A Fase 2 estende (não reescreve) a máquina de estados da Fase 1. Tudo abaixo é
coberto por teste automatizado real contra o Postgres/Temporal da fundação
(20 testes novos; suíte total **37 passed**). Migração nova:
`migrations/0009_wsb2.sql` (tabela `plan_approval_gate`).

### Nova sequência de sessões (WSB-E2-T3 estendida)

O workflow agora orquestra, dentro da fase de implementação:

```
[budget na admissão] -> Planner (read-only) -> [GATE de aprovação de plano]
  -> provision -> ( Coder -> Tester -> L1 )*  -> L2 (contexto fresco) -> PR
```

- Nomes de Activity por `dse_contracts`: `ACTIVITY_RUN_PLANNER_TURN`,
  `ACTIVITY_RUN_TESTER_TURN`, `ACTIVITY_RUN_L2_REVIEW` (import defensivo — as
  reais vêm de WS-C/WS-E; testes usam fakes com a mesma assinatura em
  `tests/fakes.py`).
- **P3 (não-negociável) no L2**: `_run_l2_review` monta o payload com
  **exatamente** `plan` + `diff_summary` + `files_changed` — nunca
  `instructions`/`clarification_notes`/`objections`/histórico do Coder.
  Provado em `test_phase2_sequence.py::test_l2_review_receives_only_plan_and_diff_not_coder_history`.
- Objeções do L2 voltam ao Coder (capadas por `l2_retry_cap`); esgotado ->
  escala. PR nunca é finalizado com objeção aberta (P6).

### Gate de aprovação de plano por risk class (WSB-E3-T2)

- Novo `@workflow.signal` **`plan_approval`** (nome = `SIGNAL_PLAN_APPROVAL`).
  Payload: `{verdict: approved|rejected, route: re_plan|re_clarify|cancel,
  comment/justification, actor}`.
- **Política vive fora do modelo (P1)**: `dse_orchestrator/policy.py`.
  `classify_risk()` faz classificação **determinística de defesa em
  profundidade** — um plano que toca `migrations/`, `.github/workflows/`,
  `auth/`, `billing/`… é `high` **mesmo que o Planner declare `low`** (um
  modelo sub-classificando não rebaixa o gate). `requires_plan_approval()`
  consulta o conjunto `require_approval_risk_classes` (config do operador,
  default `{high}`), nunca o modelo.
- `low` -> **auto-aprova por política** (nunca por ausência de aprovador).
  `high` -> estaciona no estado durável **`awaiting_plan_approval`**, resolve o
  aprovador pela **cascata CODEOWNERS -> designated approvers do access bundle
  (WS-F `dse_access_bundle`)**, renderiza o pedido via adapters
  (`post_tracking_comment`), e espera durável o `plan_approval`.
- **Cascata VAZIA = Blocked + escalação, JAMAIS auto-aprova por ausência**
  (`_finish_blocked` + audit `plan_gate_no_approver_blocked`). Aprovadores
  offboardados (`dse_console_identity.active=false`) são filtrados.
- Projeção durável consultável pelo queue board (WS-F): tabela
  `plan_approval_gate` (upsert idempotente por work_item; **não** substitui o
  audit ledger — P8: o `audit_log` continua a fonte imutável).

### Rejection path (WSB-E3-T3)

3 rotas determinísticas, sempre auditadas com **identidade + justificativa**,
e nenhuma dispara implementação sem passar de novo pelo gate correspondente:
- `re_plan` -> re-roda o Planner + gate (capado por `plan_round_cap`);
- `re_clarify` -> `continue_as_new` ao **gate de clarificação** (reabre a
  rodada limpando `acceptance_criteria`);
- `cancel` -> terminal Failed.

### Budgets na admissão e em fronteiras (WSB-E4-T1)

- `budget_max_usd` lido do JSONB `work_items.budget` (chave `max_usd`) na
  admissão; `spent_usd` **agrega o `cost_usd` reportado pelo gateway (WS-D)**
  em cada resultado de Activity de modelo (coder/tester/l2).
- Checado na admissão e em **cada fronteira de fase** (`_budget_boundary`) —
  **nunca corta no meio de uma Activity (P6)**. Exaurido -> Failed com mensagem
  clara. Operador pode elevar via `@workflow.signal raise_budget` (aplicado na
  próxima fronteira, retoma sem recomeçar). Todo evento de budget -> audit.

### Fairness por tenant, worker-side (WSB-E1-T3)

- `dse_orchestrator/fairness.py`: **interface trocável** `FairnessController`.
  `WorkerSideFairnessController` impõe um **cap de concorrência de Activity por
  tenant** (semáforo por tenant) lido de `tenant_config`
  (`fairness->>'max_concurrent_activities'` > `max_concurrent_work_items`), via
  um `FairnessInterceptor` de Activity inbound. Quando o server suportar P&F
  nativo (1.31+, indisponível — estamos em 1.29), troca-se por
  `NativeFairnessController` (no-op no worker; delega ao server via
  `fairness_key`) **sem tocar no workflow** — só na montagem do Worker
  (`--fairness-mode`).
- Teste de **burst** (`test_fairness.py`): um tenant saturando seu cap não
  empurra o dispatch do outro além do SLO (concorrência real, relógio de
  parede). Interceptor validado num Worker Temporal real (pico ≤ cap).

### Chaos do caminho de modelo + proxy fail-closed (WSB-E5-T3b)

`tests/test_chaos.py` estendido (além da queda de worker da Fase 1):
- egress-proxy indisponível / virtual key expirada / kill switch -> a Activity
  de modelo recusa **non-retryable**; o orquestrador **falha limpo na
  fronteira, sem output truncado (P6)**, auditado — via `_run_model_activity`,
  que converte a recusa de política fail-closed em `_FailClosed`;
- LiteLLM **oscilando** (transiente/retryable) -> a durabilidade do Temporal
  reexecuta a Activity e a tarefa **completa sem perder progresso** (1 PR).

### Estado `awaiting_plan_approval` — gap de enum documentado

O valor de status `awaiting_plan_approval` é gravado na coluna TEXT
`work_items.status` (sem CHECK; a própria `dse_contracts.constants` já referencia
essa string como o gatilho de roteamento do `SIGNAL_PLAN_APPROVAL` no WS-A).
Porém o **enum `dse_contracts.work_item.WorkItemStatus` (fundação, não editável
por este workstream) ainda não tem esse membro** nem o mapa público
`to_public_status` o projeta (deveria -> `"blocked"`). O orquestrador contorna
com `STATUS_AWAITING_PLAN_APPROVAL` + `_set_raw_status` (ver `workflows.py`).
**Ação de fundação recomendada**: adicionar `awaiting_plan_approval` ao enum e
ao `_PUBLIC_STATUS_MAP`.

### Gaps de fronteira honestos (a reconciliar na integração)

- **CODEOWNERS reader** (`policy.set_codeowners_reader`) é um ponto de injeção:
  produção = adapter GitHub (WS-A) lendo o arquivo via GitHub App; default local
  retorna `None` (sem GitHub). Testes injetam um fake. A cascata cai para o
  access bundle (WS-F) quando CODEOWNERS está vazio.
- **Custo do Planner** não entra no `spent_usd` (o `PlanArtifact` não carrega
  `cost_usd`); coder/tester/l2 entram. Reconciliar se WS-C passar a reportar
  custo do planner.
- **Chaos de modelo real** (LiteLLM/virtual key/egress-proxy de verdade) é
  simulado na fronteira de Activity com o **mesmo tipo de erro**
  (`ApplicationError non_retryable` vs retryable) que WS-D marca — a integração
  ponta-a-ponta real é da suíte de WS-D/WS-C.

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
    metrics.py             # Fase 3: metrica OTel dse.workflow.history_length (§3)
    workflows.py           # WorkItemLifecycleWorkflow (a maquina de estados)
    worker.py              # entrypoint: Client.connect + Worker + health endpoint
  tests/
    conftest.py            # helpers de Postgres real + fixture time_skipping_env
    fakes.py                # Activities FAKE (mesma assinatura de dse_contracts.activities;
                            # Fase 3: fakes de evidencia decodificam com os models REAIS)
    test_lifecycle_happy_path.py
    test_clarification_gate.py
    test_review_loop.py
    test_operator_controls.py
    test_chaos.py           # WSB-E5-T3 (chaos real, worker process morto)
    chaos_worker_process.py # subprocesso usado pelo chaos test
    test_evidence_pipeline.py       # Fase 3: wiring preview/demo/visual diff + failure mode 9
    test_iteration_caps_debounce.py # Fase 3: WSB-E4-T2 (caps + debounce ADR-26)
    test_history_metric.py          # Fase 3: metrica de history com InMemoryMetricReader
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
4. ~~Sem fairness/budget enforcement~~ — **implementado na Fase 2** (WSB-E1-T3
   fairness worker-side + WSB-E4 budgets). Ver a seção "Fase 2" no topo.
5. ~~Sem split Planner/Tester/Reviewer nem gate de aprovação de plano~~ —
   **implementado na Fase 2** (WSB-E2-T3 estendida + WSB-E3-T2/T3). Ver a
   seção "Fase 2" no topo.
6. **`worker.py` roda sem TLS/mTLS para o Temporal** (`Client.connect`
   simples) — adequado para o Temporal dev local; produção precisaria de
   `tls=...`/namespace real, fora do escopo desta sessão local.
