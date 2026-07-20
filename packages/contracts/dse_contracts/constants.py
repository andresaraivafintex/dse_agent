"""Constantes de integração entre workstreams — nomes fixos que todo mundo usa
para não precisar coordenar em tempo real."""

TASK_QUEUE = "dse-core-task-queue"
WORKFLOW_TYPE = "WorkItemLifecycleWorkflow"

# Nomes de signal Temporal do WorkItemLifecycleWorkflow (services/orchestrator/
# src/dse_orchestrator/workflows.py) — o Temporal Python SDK usa o nome do
# método `@workflow.signal` como nome do signal por default (sem `name=`
# explícito), então estas strings têm que bater exatamente com os nomes dos
# métodos lá. Achado da integração da Fase 1: o ingest-gateway (WS-A) e o
# review_signal (WS-E) tinham cada um sua própria constante de nome de signal
# (`"conversation_signal"` e `"review_decision"`, respectivamente) que não
# batia com NENHUM `@workflow.signal` real — todo signal enviado por eles
# era silenciosamente descartado pelo Temporal (nome sem handler registrado
# não é erro, só não aciona nada). Promovidas aqui como fonte única da
# verdade; ambos os workstreams foram corrigidos para importar daqui.
SIGNAL_CLARIFICATION_ANSWER = "clarification_answer"
SIGNAL_REVIEW_COMMENT = "review_comment"
SIGNAL_MERGED_BY_HUMAN = "merged_by_human"

# Fase 2 (adendo 01 §2.2): signal do gate de aprovação de plano (WSB-E3-T2).
# Payload: {"verdict": "approved"|"rejected", "route": "re_plan"|"re_clarify"|
# "cancel" (obrigatório quando rejected — WSB-E3-T3), "comment": str,
# "actor": principal resolvido}. O dispatcher do WS-A roteia `kind=approval`
# para este signal quando work_items.status == 'awaiting_plan_approval'
# (WSA-E6-T3) — nunca mais só pelo `kind`.
SIGNAL_PLAN_APPROVAL = "plan_approval"

# Limitação conhecida (documentar, não esconder — P8): o roteamento correto
# de um ConversationEvent correlacionado (Path B) deveria depender do ESTADO
# atual do WorkItem (esperando clarificação? esperando review?), não só do
# `kind` do evento — um `kind=approval` vindo de um botão Slack, por exemplo,
# hoje é mapeado heuristicamente para SIGNAL_REVIEW_COMMENT no dispatcher do
# WS-A porque a Fase 1 não tem gate de aprovação de plano (isso é Fase 2,
# WSB-E3-T2) — se/quando ele existir, este mapeamento precisa consultar
# `work_items.status` em vez de decidir só pelo `kind`.

# Atributos de span OTel que WSD-E3 (model-gateway) emite e WSF-E7 (dashboards/
# alerting) consome — contrato entre WS-D e WS-F sem precisar rodar juntos.
OTEL_ATTR_TENANT = "dse.tenant_id"
OTEL_ATTR_WORK_ITEM = "dse.work_item_id"
OTEL_ATTR_STAGE = "dse.stage"
OTEL_ATTR_MODEL = "dse.model"
OTEL_ATTR_COST_USD = "dse.cost_usd"
OTEL_ATTR_TOKENS_IN = "dse.tokens_in"
OTEL_ATTR_TOKENS_OUT = "dse.tokens_out"
# Promovido do WS-D na Fase 2 (adendo 01 §4) — já era emitido como atributo
# ad-hoc "dse.task_class" na Fase 1.
OTEL_ATTR_TASK_CLASS = "dse.task_class"
