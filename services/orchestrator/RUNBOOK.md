# RUNBOOK — Worker Versioning e drain-and-cutover (WSB-E1-T2)

Este runbook cobre como fazer deploy de uma nova versao do `orchestrator`
worker sem perder execucoes de workflow em andamento e sem correr o risco de
um worker antigo (com codigo de workflow desatualizado) tentar fazer replay
de historico gerado por codigo novo (ou vice-versa) — a causa classica de
`NonDeterministicWorkflowError` no Temporal.

## 1. Conceito: `build_id` fixo por deploy

Todo processo do worker (`worker.py`) precisa de um `--build-id` (ou
`DSE_WORKER_BUILD_ID`) **fixo e unico por artefato de deploy** — normalmente
o SHA curto do commit ou a tag da imagem de container:

```bash
export DSE_WORKER_BUILD_ID=$(git rev-parse --short HEAD)
python -m dse_orchestrator.worker --build-id "$DSE_WORKER_BUILD_ID"
```

**Nunca** reutilize um `build_id` entre deploys com codigo de workflow
diferente — isso e o que a garantia de versionamento do Temporal usa para
decidir quais workers podem continuar o historico de quais execucoes.

## 2. Dois modos suportados

### 2.1 Modo simples (default, Fase 1): sem Worker Versioning ativo

Por default (`DSE_WORKER_USE_VERSIONING=false`), o worker roda com
`build_id` apenas para fins de `identity`/observabilidade (aparece no
Temporal UI, facilita saber qual deploy processou qual task), **sem**
ativar o enforcement de compatibilidade de versao do servidor. Isso e
apropriado para a Fase 1 (times pequenos, deploys pouco frequentes,
mudancas de workflow raras) e evita a complexidade operacional adicional de
gerenciar "Build ID sets" no servidor.

**Procedimento de deploy neste modo (drain manual via `max_cached_workflows`
+ graceful shutdown):**

1. Suba o worker NOVO (`build_id` novo) apontando para a MESMA task queue.
   Agora ha 2 workers pollando a mesma fila.
2. Envie `SIGTERM` para o worker ANTIGO. O `worker.py` trata `SIGTERM`
   fazendo `async with worker: await stop_event.wait()` sair do bloco `async
   with`, o que aciona o graceful shutdown nativo do SDK (`Worker.shutdown`):
   para de pollar novas tasks, mas espera as **workflow tasks e activities em
   andamento terminarem** antes de encerrar o processo.
3. So depois que o worker antigo confirmar shutdown limpo (log "worker
   parado"), remova-o da infra (kill do container/processo).
4. **Risco residual deste modo:** se o codigo do workflow mudou de forma
   NAO-compativel (removeu/reordenou um `await` ou signal handler que ja
   apareceu no historico de uma execucao em andamento), uma execucao que
   estava "no meio" quando o worker antigo caiu pode, ao ser retomada pelo
   worker novo, falhar com `NonDeterministicWorkflowError`. Mitigar
   restringindo mudanas de workflow a apenas *adicoes* aditivas
   (novos signals, novos ramos usando `workflow.patched()`) durante
   deploys— ver secao 3.

### 2.2 Modo com Worker Versioning classico ativo

Ative com `DSE_WORKER_USE_VERSIONING=true` (ou `--use-worker-versioning`).
Isso passa `use_worker_versioning=True` ao `Worker`, o que exige que o
`build_id` deste worker esteja registrado como compativel na task queue
**antes** dele comecar a pollar:

```bash
# Primeiro deploy (build A) — registra A como o build "default" da fila:
temporal task-queue update-build-ids add-new-default \
  --task-queue dse-core-task-queue \
  --build-id build-A

# Deploy seguinte (build B), compativel com A (sem mudanca quebrando
# determinismo) — registra B como novo default, mantendo A disponivel para
# execucoes antigas ainda em voo (elas NAO migram para B automaticamente
# aqui; usam a versao registrada quando comecaram):
temporal task-queue update-build-ids add-new-default \
  --task-queue dse-core-task-queue \
  --build-id build-B
```

**Drain-and-cutover com este modo:**

1. Rode o `temporal task-queue update-build-ids add-new-default` do build
   NOVO — isso NAO derruba o worker antigo; so faz **novas** execucoes
   passarem a usar o build novo.
2. Mantenha o worker ANTIGO no ar ate o Temporal UI mostrar zero execucoes
   abertas atribuidas ao `build_id` antigo (`tmprl.server.buildIds` na busca
   avancada, ou `temporal task-queue get-build-ids` para ver quais builds
   ainda tem execucoes abertas).
3. So entao pare o worker antigo (`SIGTERM`, mesmo procedimento do modo
   simples).

Este modo e mais seguro para deploys frequentes/times maiores, mas exige
disciplina operacional adicional (o passo 1 e uma chamada de API/CLI
explicita, nao acontece sozinho). Deixamos DESLIGADO por default na Fase 1
por ser P7 (boring-first) — ativar quando o ritmo de deploy justificar.

## 3. Mudancas de workflow seguras vs. inseguras

**Seguro (pode fazer deploy simples, sem drain especial):**
- Adicionar um novo `@workflow.signal`/`@workflow.query`.
- Adicionar codigo NOVO no fim de uma fase que ja termina com
  `continue_as_new` ou `return` (nao afeta workflows em execucoes ja
  fechadas por continue_as_new).
- Mudar Activities chamadas por NOME (a implementacao pode mudar livremente
  do lado do worker que a executa, sem afetar o historico do workflow, que
  so registra o *nome* e os *argumentos/resultado* serializados).

**Inseguro (requer o drain cuidadoso da secao 2, ou `workflow.patched()`):**
- Remover, reordenar, ou trocar a condicao de um `await` existente no meio
  de uma fase que ainda tem execucoes abertas.
- Mudar o payload/schema de `WorkItemLifecycleInput` de forma
  incompativel (remover um campo com default, mudar um tipo) sem
  compatibilidade retroativa — nossos dataclasses tem defaults em quase
  todo campo justamente para tolerar isso melhor.

## 4. Verificacao pos-deploy

```bash
curl -s http://localhost:8900/health
# {"status":"ok","build_id":"<esperado>","task_queue":"dse-core-task-queue"}
```

Confirme no Temporal UI (`http://localhost:8088`) que a task queue
`dse-core-task-queue` tem pollers ativos e que nenhuma execucao ficou presa
em `WorkflowTaskFailed` repetido (sintoma de nao-determinismo).
