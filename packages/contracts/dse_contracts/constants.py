"""Constantes de integração entre workstreams — nomes fixos que todo mundo usa
para não precisar coordenar em tempo real."""

TASK_QUEUE = "dse-core-task-queue"
WORKFLOW_TYPE = "WorkItemLifecycleWorkflow"

# Atributos de span OTel que WSD-E3 (model-gateway) emite e WSF-E7 (dashboards/
# alerting) consome — contrato entre WS-D e WS-F sem precisar rodar juntos.
OTEL_ATTR_TENANT = "dse.tenant_id"
OTEL_ATTR_WORK_ITEM = "dse.work_item_id"
OTEL_ATTR_STAGE = "dse.stage"
OTEL_ATTR_MODEL = "dse.model"
OTEL_ATTR_COST_USD = "dse.cost_usd"
OTEL_ATTR_TOKENS_IN = "dse.tokens_in"
OTEL_ATTR_TOKENS_OUT = "dse.tokens_out"
