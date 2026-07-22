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

### 2.2 Modo com Worker Deployment Versioning (moderno — plano 08 §F/F5)

> **Atualizacao (plano 08 §F, F5):** trocamos a **version-set classica**
> (`update-build-ids`, deprecada e DESLIGADA por default nos servers atuais —
> `RPCError: Worker versioning v0.1 ... is disabled`) pelo **Worker Deployment
> Versioning** moderno. O worker se anuncia como a versao `(deployment_name,
> build_id)` com `default_versioning_behavior=PINNED` (`build_deployment_config`
> em `worker.py`). PINNED = cada workflow fica GRUDADO na versao em que comecou.

Ative com `DSE_WORKER_USE_VERSIONING=true` (ou `--use-worker-versioning`).
Pine `DSE_WORKER_BUILD_ID` ao SHA/tag da imagem e `DSE_WORKER_DEPLOYMENT_NAME`
(default `dse-orchestrator`). Pre-requisito de server: deployment versioning
habilitado no namespace.

**Drain-and-cutover com este modo:**

```bash
# 1. Suba o worker do build NOVO (ex.: build_id=git-B) apontando p/ a MESMA
#    task queue. Ele se registra como uma nova VERSAO do deployment, mas ainda
#    NAO é a corrente — nenhum workflow novo vai p/ ele ainda.

# 2. CUTOVER (passo deliberado de operacao — NUNCA automatico no boot):
temporal worker-deployment set-current-version \
  --deployment-name dse-orchestrator \
  --build-id git-B
#    A partir daqui, workflows NOVOS vao para git-B. Os EM VOO continuam em
#    git-A (PINNED) ate terminarem — é o drain seguro.

# 3. Aguarde zero execucoes abertas na versao antiga (Temporal UI → Deployments,
#    ou `temporal worker-deployment describe --deployment-name dse-orchestrator`),
#    entao pare o worker antigo (SIGTERM, graceful drain).
```

DESLIGADO por default (P7 boring-first + requer server habilitado): o passo 2
é uma chamada de CLI explicita, nao acontece sozinho — evita cutover acidental
a cada restart. Ative quando o ritmo de deploy justificar.

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
