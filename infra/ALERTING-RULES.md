# Alerting rules — Fintex DSE (WSF-E7-T1)

Owner: WS-F. No real alerting backend (PagerDuty/Opsgenie/Alertmanager
with configured receivers) is provisioned in this dev session — this
document is the accepted substitute (per the task's scope) until a real
backend is chosen by the client/program. The rules below are specified against
the span/metric attributes that the other services already emit or are expected to emit
(contract: `dse_contracts.constants.OTEL_ATTR_*`),
so that any real backend (Grafana/Datadog/Honeycomb/Alertmanager)
can implement them directly by translating the condition into its native
query syntax.

All rules assume the attributes already published in
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

## 1. Budget exhaustion (per tenant)

- **Condition**: the sum of `dse.cost_usd` (spans carrying that attribute, emitted
  by the model-gateway on every LLM call) grouped by `dse.tenant_id`, over a
  30-day sliding window, exceeds `tenant_config.monthly_budget_usd`
  (Postgres, `migrations/0007_wsf.sql`).
- **Severity**:
  - **Warning** at 80% of budget — notifies the tenant owner (the program's
    internal Slack channel, not the client yet).
  - **Critical** at 100% of budget — trips the tenant's `kill_switch_enabled`
    (`tenant_config`) via a deterministic automated action (P1: the
    decision to block is a threshold rule in code, never an LLM) and
    writes a row to `audit_log` (`action='budget_exhausted_kill_switch'`,
    via `dse_audit.emit`, actor `system:budget-monitor`).
- **Real data source today**: no metric exporter is aggregating
  this yet (this repo's `otel-collector` only does `debug` export/stdout —
  see `infra/otel-collector-config.yaml`). Production: add a
  Prometheus/otlphttp exporter + a real recording/alerting rule.

## 2. Unresolved egress denies

- **Condition**: every egress-proxy denial (WS-C) produces an
  `audit_log` row (expected `action`: `egress_denied` or equivalent —
  the exact contract to be settled with WS-C at integration). The alert fires when
  there are **N or more denials for the same `work_item_id`/tenant within a
  5-minute window with no subsequent operator action** (e.g. a manual pause,
  an escalation) — this indicates a possible active exfiltration attempt or a
  buggy sandbox retrying indefinitely against a blocked host.
- **Severity**: Critical — this is literally the structural containment
  against attack class #1 of the master plan (indirect prompt
  injection leading to exfiltration via egress); an isolated denial is
  expected (that is the proxy working), but a pattern of repeated,
  uninvestigated denials is the real warning sign.
- **Verification available today**: `dse_audit.reconstruct_work_item_history(work_item_id)`
  (WSF-E1-T2, `packages/dse_audit/dse_audit/queries.py`) already allows
  querying all of a WorkItem's actions manually, including egress
  denials, as soon as WS-C starts writing them — no additional code
  is needed on the query side.

## 3. Approaching the Temporal history limit — **ENABLED (Phase 3)**

> **Status: rule ACTIVE in the collector** (no longer just a specification). A dedicated
> `metrics/history_alert` pipeline in `infra/otel-collector-config.yaml`:
> a `filter` (OTTL) drops everything below the threshold, a `transform` tags
> what remains with `dse.alert=temporal_history_threshold_exceeded` +
> `dse.alert_severity=warning|critical`, and the `debug/history_alert` exporter
> prints it — the presence of the line in the collector's stdout IS the alert (MVP).
> Real proof: `services/platform/tests/test_history_alert.py` (sends real OTLP
> above/below the threshold and checks the channel).
>
> **Metric-name contract with WS-B** (emit_history_metric): the filter
> accepts `dse.workflow.history_length`/`temporal_workflow_event_history_length`
> (event count) and `dse.workflow.history_size_bytes`/
> `temporal_workflow_event_history_size` (bytes). Active thresholds:
> events ≥ 35,840 (70% of 51,200) = warning, ≥ 46,080 (90%) = critical;
> bytes ≥ 36,700,160 (70% of 50MB) = warning, ≥ 47,185,920 (90%) = critical.
> Recommendation: pin ONE canonical name in `dse_contracts.constants` in the
> next contract window (request filed; we do not edit the foundation
> unilaterally).
>
> **Upgrade to real alerting (documented, not hidden):** keep
> `filter/history_alert` + `transform/history_alert` and swap the
> `debug/history_alert` exporter for `prometheusremotewrite` (+ an alert rule in
> Alertmanager) or for the native exporter of the client's backend. No
> change on the emitting side.

- **Condition**: Temporal recommends keeping a workflow's event history
  below ~10,000 events / 50MB (hard limits at ~51,200 events / 50MB
  by cluster default) — a long-running `WorkItemLifecycleWorkflow`
  (multiple clarification/steering rounds) can approach that.
  Alert when `temporal_workflow_event_history_size` (a native metric of the
  Temporal SDK/server) exceeds 70% of the configured limit for ANY
  workflow with an active `dse.work_item_id`.
- **Severity**: Warning at 70%, Critical at 90% — leaving time for the
  workflow to `Continue-As-New` (structural mitigation, WS-B's
  responsibility in the workflow design) before hitting the hard limit
  (which would fail the workflow).
- **Real data source**: Temporal exposes this natively via its own
  internal metric (`temporal_workflow_event_history_size` as a `histogram`,
  available on the frontend/history service metrics endpoint) — it does
  not depend on any additional WS-D instrumentation, the Temporal server
  itself emits it. Production: scrape it via Prometheus (the foundation's
  `docker-compose.yml` does not expose Temporal's Prometheus metrics
  yet — adding `--metrics-port` and the scrape config during the
  otel-collector upgrade is recorded here as an explicit TODO, not
  hidden).

## What is missing for production (honest, not hidden)

- No real alerting backend is connected — this is rule documentation,
  not active alerts. Real integration requires: (a) choosing the
  client's backend (Grafana Alerting / Datadog Monitors / Alertmanager);
  (b) swapping the `otel-collector`'s `debug` exporter for a real exporter
  (`prometheusremotewrite`, `otlphttp`, or the native exporter of the chosen
  backend); (c) translating the 3 rules above into the chosen backend's
  query syntax.
- Rule 1 (budget) depends on `tenant_config.monthly_budget_usd` already
  existing (done, `migrations/0007_wsf.sql`) but NO service writes
  `dse.cost_usd` from a real provider cost yet (it depends on WS-D
  having a real instrumented Bedrock call — with no AWS/Bedrock account
  provisioned in this session, WS-D uses the local `eco/echo-model` tier).
- Rule 2 depends on WS-C naming and writing the exact egress-denial
  `action` in the audit_log — the exact name (`egress_denied` vs something else) must be
  confirmed at integration (not invented unilaterally here).
