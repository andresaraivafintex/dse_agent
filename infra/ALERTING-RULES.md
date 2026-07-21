# Alerting rules — Fintex DSE (WSF-E7-T1)

Dono: WS-F. Nenhum backend de alerting real (PagerDuty/Opsgenie/Alertmanager
com receivers configurados) está provisionado nesta sessão de dev — este
documento é o substituto aceitável (conforme escopo da tarefa) até que um
backend real seja escolhido pelo cliente/programa. As regras abaixo são
especificadas contra os atributos de span/métrica que os outros serviços já
emitem ou devem emitir (contrato: `dse_contracts.constants.OTEL_ATTR_*`),
de forma que qualquer backend real (Grafana/Datadog/Honeycomb/Alertmanager)
possa implementá-las diretamente traduzindo a condição para sua sintaxe de
query nativa.

Todas as regras assumem os atributos já publicados em
`packages/contracts/dse_contracts/constants.py`:

```
OTEL_ATTR_TENANT     = "dse.tenant_id"
OTEL_ATTR_WORK_ITEM  = "dse.work_item_id"
OTEL_ATTR_STAGE      = "dse.stage"
OTEL_ATTR_MODEL      = "dse.model"
OTEL_ATTR_COST_USD   = "dse.cost_usd"
OTEL_ATTR_TOKENS_IN  = "dse.tokens_in"
OTEL_ATTR_TOKENS_OUT = "dse.tokens_out"
```

## 1. Exaustão de budget (por tenant)

- **Condição**: soma de `dse.cost_usd` (spans com esse atributo, emitidos
  pelo model-gateway a cada chamada LLM) agrupada por `dse.tenant_id`, numa
  janela deslizante de 30 dias, ultrapassa `tenant_config.monthly_budget_usd`
  (Postgres, `migrations/0007_wsf.sql`).
- **Severidade**:
  - **Warning** em 80% do budget — notifica o owner do tenant (canal
    Slack interno do programa, não o cliente ainda).
  - **Critical** em 100% do budget — dispara o `kill_switch_enabled` do
    tenant (`tenant_config`) via ação automatizada determinística (P1: a
    decisão de bloquear é uma regra de threshold em código, nunca um LLM) e
    gera uma linha em `audit_log` (`action='budget_exhausted_kill_switch'`,
    via `dse_audit.emit`, ator `system:budget-monitor`).
- **Fonte de dados real hoje**: nenhum exporter de métrica está agregando
  isto ainda (o `otel-collector` deste repo só faz `debug` export/stdout —
  ver `infra/otel-collector-config.yaml`). Produção: adicionar um exporter
  Prometheus/otlphttp + uma regra de recording/alerting real.

## 2. Egress denies não resolvidos

- **Condição**: toda negação do egress-proxy (WS-C) gera uma linha de
  `audit_log` (`action` esperado: `egress_denied` ou equivalente —
  contrato exato a fechar com WS-C na integração). Alerta dispara quando
  existem **N ou mais negações do mesmo `work_item_id`/tenant em uma janela
  de 5 minutos sem uma ação de operador subsequente** (ex.: pause manual,
  escalonamento) — indica possível tentativa ativa de exfiltração ou
  sandbox com bug fazendo retry indefinido contra um host bloqueado.
- **Severidade**: Critical — este é literalmente a contenção estrutural
  contra a classe de ataque nº 1 do plano mestre (prompt injection
  indireta levando a exfiltração via egress); uma negação isolada é
  esperada (é o proxy funcionando), mas um padrão de negações repetidas e
  não investigadas é o sinal de alerta real.
- **Verificação real disponível hoje**: `dse_audit.reconstruct_work_item_history(work_item_id)`
  (WSF-E1-T2, `packages/dse_audit/dse_audit/queries.py`) já permite
  consultar manualmente todas as ações de um WorkItem, incluindo negações
  de egress, assim que o WS-C começar a gravá-las — nenhum código adicional
  necessário no lado de consulta.

## 3. Aproximação do limite de history do Temporal — **ATIVADA (Fase 3)**

> **Status: regra ATIVA no collector** (não mais só especificação). Pipeline
> dedicada `metrics/history_alert` em `infra/otel-collector-config.yaml`:
> um `filter` (OTTL) descarta tudo abaixo do threshold, um `transform` marca
> o que sobra com `dse.alert=temporal_history_threshold_exceeded` +
> `dse.alert_severity=warning|critical`, e o exporter `debug/history_alert`
> imprime — a presença da linha no stdout do collector É o alerta (MVP).
> Prova real: `services/platform/tests/test_history_alert.py` (envia OTLP
> real acima/abaixo do threshold e verifica o canal).
>
> **Contrato de nome de métrica com o WS-B** (emit_history_metric): o filtro
> aceita `dse.workflow.history_length`/`temporal_workflow_event_history_length`
> (nº de eventos) e `dse.workflow.history_size_bytes`/
> `temporal_workflow_event_history_size` (bytes). Thresholds ativos:
> eventos ≥ 35.840 (70% de 51.200) = warning, ≥ 46.080 (90%) = critical;
> bytes ≥ 36.700.160 (70% de 50MB) = warning, ≥ 47.185.920 (90%) = critical.
> Recomendação: pinar UM nome canônico em `dse_contracts.constants` na
> próxima janela de contrato (pedido registrado; não editamos a fundação
> unilateralmente).
>
> **Upgrade para alerting real (documentado, não escondido):** manter
> `filter/history_alert` + `transform/history_alert` e trocar o exporter
> `debug/history_alert` por `prometheusremotewrite` (+ regra de alerta no
> Alertmanager) ou pelo exporter nativo do backend do cliente. Nenhuma
> mudança em quem emite.

- **Condição**: Temporal recomenda manter o event history de um workflow
  abaixo de ~10.000 eventos / 50MB (limites hard em ~51.200 eventos / 50MB
  por padrão do cluster) — um `WorkItemLifecycleWorkflow` de longa duração
  (múltiplas rodadas de clarificação/steering) pode se aproximar disso.
  Alerta quando `temporal_workflow_event_history_size` (métrica nativa do
  Temporal SDK/servidor) ultrapassa 70% do limite configurado para QUALQUER
  workflow com `dse.work_item_id` ativo.
- **Severidade**: Warning em 70%, Critical em 90% — dar tempo para o
  workflow fazer `Continue-As-New` (mitigação estrutural, responsabilidade
  do WS-B no design do workflow) antes de atingir o hard limit (que causaria
  falha do workflow).
- **Fonte de dados real**: Temporal expõe isto nativamente via sua própria
  métrica interna (`temporal_workflow_event_history_size` em `histogram`,
  disponível no endpoint de métricas do frontend/history service) — não
  depende de nenhuma instrumentação adicional do WS-D, é o próprio servidor
  Temporal que emite. Produção: scrape isso via Prometheus (o
  `docker-compose.yml` da fundação não expõe métricas Prometheus do
  Temporal ainda — adicionar `--metrics-port` e o scrape config no upgrade
  do otel-collector fica registrado aqui como TODO explícito, não
  escondido).

## O que falta para produção (honesto, não escondido)

- Nenhum backend de alerting real está conectado — isto é documentação de
  regras, não alertas ativos. Integração real requer: (a) escolher o
  backend do cliente (Grafana Alerting / Datadog Monitors / Alertmanager);
  (b) trocar o exporter `debug` do `otel-collector` por um exporter real
  (`prometheusremotewrite`, `otlphttp`, ou o exporter nativo do backend
  escolhido); (c) traduzir as 3 regras acima para a sintaxe de query do
  backend escolhido.
- A regra 1 (budget) depende de `tenant_config.monthly_budget_usd` já
  existir (feito, `migrations/0007_wsf.sql`) mas NENHUM serviço grava
  `dse.cost_usd` a partir de custo real de provider ainda (depende do WS-D
  ter uma chamada Bedrock real instrumentada — sem conta AWS/Bedrock
  provisionada nesta sessão, WS-D usa o tier `eco/echo-model` local).
- A regra 2 depende do WS-C nomear e gravar o `action` exato de negação de
  egress no audit_log — o nome exato (`egress_denied` vs outro) precisa ser
  confirmado na integração (não inventado unilateralmente aqui).
