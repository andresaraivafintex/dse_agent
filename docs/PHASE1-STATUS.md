# Fase 1 ("Core loop") — Status da implementação

Data: 2026-07-10. Escopo: implementação real (não apenas planejamento) da Fase 1 do
[plano mestre](../../plano-desenvolvimento/00-PLANO-MESTRE.md) — 6 workstreams construídos em
paralelo sobre uma fundação de contratos compartilhados, depois integrados, testados e
corrigidos neste repositório (`fase1/`).

## Resumo executivo

- **223 testes passando, 2 pulados (com razão explícita), 0 falhando**, cobrindo os 10
  pacotes/serviços do monorepo, rodados contra Postgres/Temporal/Docker **reais** (nunca
  mockados para as garantias de durabilidade/idempotência).
- Um **smoke test end-to-end real** (sem nenhum atalho manual) foi executado com sucesso:
  Slack/GitHub → outbox transacional → dispatcher → workflow Temporal real → gate de
  clarificação → resposta correlacionada (Path B) → sandbox Docker real provisionado e
  isolado. Ver §"Smoke test" abaixo.
- **9 bugs de integração reais foram encontrados e corrigidos** durante a consolidação —
  nenhum workstream individual os teria pego sozinho, porque cada um só validou sua própria
  fatia contra fakes/stubs. Ver §"Achados da integração".
- **2 lacunas genuínas ficam abertas** (não escondidas): os cenários mínimos de chaos do
  caminho de modelo/proxy, e o gatilho real de `merged_by_human`. Ver §"O que falta".

## O que foi construído (real, testável, rodando localmente)

| Workstream | Serviços | O que funciona de verdade |
|---|---|---|
| WS-A | `adapter-slack`, `adapter-github`, `ingest-gateway` | Outbox transacional, dispatcher com `SELECT...FOR UPDATE SKIP LOCKED` provado sob concorrência real, as 4 defesas de intake (assinatura, TOCTOU, sanitização, dedup), correlação Path A/B, steering allowlist |
| WS-B | `orchestrator` | Worker Temporal real, máquina de estados completa do WorkItem, gate de clarificação com timers, loop de review humano sem nenhum path de merge automático (verificado estaticamente), checkpoint/recovery, controles de operador, **chaos test que mata um worker Temporal de verdade e prova recuperação sem perda/duplicação** |
| WS-C | `sandbox-runtime`, `egress-proxy` | Containers Docker rootless reais (sem docker.sock, sem root, rede isolada), proxy de egress default-deny real, credenciais efêmeras, adapter OpenHands real (`openhands-sdk` instalado e funcional) |
| WS-D | `model-gateway` | LiteLLM real rodando, modelo "eco" determinístico para teste sem custo, virtual keys reais (mint/revoke via API do LiteLLM), spans de custo OTel |
| WS-E | `validation` | Pipeline L1 real (lint/typecheck/test/build + `bandit` SAST + secret-scan + diff-budget/forbidden-paths vs `PlanArtifact`), PR finalizer idempotente, consumo de status de CI, resume do workflow por review comment |
| WS-F | `platform` | Audit ledger com reconstrução por auditoria provada, cliente Vault real, scanner de secrets em texto plano (repo inteiro limpo), Helm charts validados (`helm lint`/`helm template`), CI de plataforma |

## Smoke test (rodado nesta sessão, sem atalhos manuais)

1. `admit_work_item()` real admite uma tarefa incompleta (sem critério de aceite) →
   `work_items`+`ingest_events` gravados numa transação.
2. O dispatcher (container real, rodando `run_forever`) drena o outbox e chama
   `Temporal.start_workflow` real.
3. O `WorkItemLifecycleWorkflow` real roda a checagem de completude e pede clarificação
   (`clarification_requested`, `missing: [acceptance_criteria]`) — audit row gravada.
4. `record_signal_event()` real grava uma resposta de clarificação correlacionada ao mesmo
   `work_item_id` (Path B).
5. O dispatcher drena esse segundo evento e sinaliza o workflow com o **nome e formato de
   payload corretos** (corrigidos nesta sessão — ver achados nº 3–5 abaixo).
6. O workflow recebe a resposta, marca `clarification_complete`, e chama
   `provision_sandbox` de verdade — **um container Docker real é criado**, isolado na rede
   `dse_sandbox_net` (sem gateway de internet, só alcança o `egress-proxy`).
7. `WorkItem.status` chega a `implementing`. Neste ponto o próximo passo real (`git clone` do
   `acme/demo-repo`, um repositório que não existe) é o limite honesto do que dá para provar
   sem uma GitHub App registrada e um repositório de teste real — parada esperada, não uma
   falha do sistema.

Repetido 2x (incluindo depois de recriar o container do Temporal do zero, para provar que a
correção do dynamicconfig — achado nº 2 — é real e não um acaso) com o mesmo resultado.

## Achados da integração (o valor real de rodar tudo junto)

Cada um destes só apareceu ao ligar os 6 workstreams entre si — todos os testes
*individuais* de cada workstream já passavam antes destas correções:

1. **Conflito de rede Docker** (`dse_sandbox_net` criada em runtime pelo WS-C colidia com a
   declaração do compose do WS-C) — corrigido: rede referenciada como `external: true`.
2. **Temporal não subia de verdade** (`DYNAMIC_CONFIG_FILE_PATH` apontava para um arquivo
   inexistente na imagem; um `docker cp` manual "colava" só porque o container nunca era
   recriado) — corrigido na fundação para apontar ao `docker.yaml` que já existe na imagem;
   validado recriando o container do zero.
3. **Activities do WS-C/WS-E nunca eram registradas no worker real**: o loader do WS-B
   procurava um nome de módulo (`validation.activities`) e um atributo (`ACTIVITIES`) que não
   batiam com o que WS-C (`sandbox_runtime.activities`, sem nenhuma lista exportada) e WS-E
   (`dse_validation.activities`, exportava `ALL_ACTIVITIES`) realmente publicavam — as 8
   Activities cross-workstream eram silenciosamente ignoradas. Corrigido nos 3 pontos;
   confirmado no log real: *"Worker no ar. 12 activities registradas."*
4. **3 nomes de signal Temporal diferentes para o mesmo conceito**: WS-A usava
   `"conversation_signal"` (genérico), WS-E usava `"review_decision"`, e nenhum dos dois batia
   com os `@workflow.signal` reais do WS-B (`clarification_answer`, `review_comment`,
   `merged_by_human`). Todo signal enviado pelos caminhos automáticos era descartado
   silenciosamente pelo Temporal (nome sem handler não é erro, só não faz nada). Promovidos a
   constantes únicas em `dse_contracts.constants` e corrigidos nos dois lados.
5. **Formato de payload incompatível** mesmo depois de corrigir o nome: o workflow lê
   `payload["verdict"]`/`["comment"]` (review) e `payload["text"]`/`["acceptance_criteria"]`
   (clarificação); WS-A repassava o `ConversationEvent` bruto e WS-E enviava `{"decision":...}`
   — nenhuma das chaves batia. Corrigido com uma função de tradução explícita no dispatcher
   (`_build_signal_payload`) e no `review_signal.py`.
6. **Heurística de clarificação**: mesmo com o nome/formato corretos, uma resposta de
   clarificação livre (texto humano) nunca preenchia `acceptance_criteria` porque não existe
   nenhuma etapa de extração estruturada na Fase 1 — o gate reciclava a mesma pergunta para
   sempre. Corrigido com uma heurística documentada (qualquer resposta não-vazia satisfaz o
   único item do checklist que não seja repo/branch) — **limitação genuína registrada, não
   escondida**: um checklist multi-campo por task-class é trabalho futuro.
7. **Race condition real em `Dispatcher.drain_all()`**: sob concorrência de 2 dispatchers
   reais, um round podia ver 0 linhas só porque o outro segurava o lock das últimas
   disponíveis naquele instante — não significa fila vazia, mas a heurística de saída parava
   ali, deixando linhas sem processar (reproduzido: 12 de 20 processadas). Mitigado (exigir 2-3
   rounds vazios consecutivos + backoff); documentado como heurística probabilística, não uma
   garantia formal — em produção o dispatcher roda via `run_forever` (loop contínuo), que não
   tem esse problema.
8. **Teste do WS-F assumia uma rota `/health` REST** no egress-proxy do WS-C, que na verdade é
   um forward proxy HTTP/CONNECT bruto (sem rotas) — corrigido para verificar o comportamento
   real (recusa limpa de request malformado com 400).
9. **Uma chave hardcoded** (`api_key: "sk-eco-local-dev-not-a-real-key"`) no
   `litellm_config.yaml` do modelo eco (não é uma credencial real, mas quebrava a convenção do
   resto do arquivo) — movida para indireção `os.environ/...`; o scanner de secrets do WS-F
   agora reporta o repositório inteiro limpo.

## O que falta para a Fase 1 ser considerada "exited" (Seção 16, honestamente)

| Critério de saída | Status |
|---|---|
| UC1/UC3 verdes em repo interno (incl. gate PR-opened/CI-green) | **Parcial** — mecânica completa e testada com fakes; falta rodar contra uma GitHub App e repo reais (nenhuma credencial disponível nesta sessão) |
| Chaos test NFR-01 (dispatcher/worker mid-flight) | **Atendido** — as duas metades (WS-A concorrência de dispatcher, WS-B kill de worker real) provadas |
| Chaos: cenários mínimos de falha do caminho de modelo + proxy indisponível fail-closed | **Não atendido** — nenhum teste cobre gateway indisponível/chave expirada mid-task nem egress-proxy fora do ar; gap real, não construído por falta de tempo nesta sessão |
| Primeiro exercício de reconstrução por auditoria | **Atendido** — `reconstruct_work_item_history` testado com uma sequência completa |
| Atribuição de custo operante | **Parcial** — spans reais com custo/tokens; agregação hoje é em memória por processo, produção precisa do OTel collector (já hospedado pelo WS-F, faltando só a integração) |
| Nenhum secret em sandbox / secrets só via backend mínimo | **Atendido** — provado por teste (WS-C) e por scanner de repositório (WS-F, 0 achados) |

Outras lacunas conhecidas, documentadas nos READMEs de cada serviço:
- **`merged_by_human` nunca é disparado de verdade** — não existe handler de webhook
  `pull_request` (`action=closed`, `merged=true`) em `adapter-github`; o terceiro pause point
  (espera de merge) funciona no workflow mas nada hoje o aciona automaticamente.
- Nenhuma credencial real (Slack App, GitHub App, conta AWS/Bedrock) foi usada — tudo com
  fallback fixture claramente sinalizado (`fixture=True` nos logs/testes relevantes).
- Cluster K8s do cliente: Helm charts validados localmente (`helm lint`/`helm template`), nunca
  aplicados contra um cluster real.

## Como rodar

```
cd fase1
make up        # sobe toda a infra + os 8 serviços (build automático das imagens)
make migrate   # aplica migrations/*.sql (idempotente)
# testes: cada workstream tem seu próprio venv (.venv-wsa .. .venv-wsf, ver CONVENTIONS.md)
```

Portas: Temporal UI `:8088`, Vault `:8200`, model-gateway `:4000`, adapters `:8801-8803`,
orchestrator health `:8900`, egress-proxy `:8806`.
