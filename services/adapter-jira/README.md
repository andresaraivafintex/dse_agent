# adapter-jira (WS-A, Phase 2 — WSA-E5)

The DSE's third intake surface (§10.1, UC2/UC5), mirroring the structure of
`adapter-github`. All shared logic (admission, correlation, the 4
intake defenses, tenant binding) comes from `ingest_gateway` — the adapter is 100%
stateless. General WS-A documentation lives in
[`../ingest-gateway/README.md`](../ingest-gateway/README.md); this file
covers only what is Jira-specific.

## What it does

- **Inbound webhook** (`POST /jira/webhook`, `adapter_jira/app.py`):
  - `jira:issue_created` / `jira:issue_updated` carrying the trigger label (`dse`) ->
    `task_request`.
  - A status transition into the configured approval column
    (`JIRA_PLAN_APPROVED_STATUS`, e.g. "Plan approved") -> `kind=approval`
    with `approval_verdict=approved` (UC5 on the Jira surface). The rejection
    column (`JIRA_PLAN_REJECTED_STATUS`) -> `approval_verdict=rejected` +
    `approval_route=re_plan`. The dispatcher (WSA-E6-T3) routes this to
    `SIGNAL_PLAN_APPROVAL` when the WorkItem is in `awaiting_plan_approval`.
  - `comment_created` -> `clarification_answer`, correlated by ticket key.
  - Correlation by **ticket key** (`source_ref = {"ticket_key": "DSE-123"}`).
  - The 4 defenses: signature (`X-Hub-Signature` HMAC-SHA256,
    `ingest_gateway.verify_jira_signature`), TOCTOU snapshot (content read from the
    payload, never re-fetched), sanitization (`sanitize_content`), idempotency
    (deterministic `event_id`).

- **MANDATORY fallback poller** (`adapter_jira/poller.py`,
  `python -m adapter_jira.poller_main`): the Jira webhook is best-effort. The poller
  sweeps the configured projects and reconciles each issue through the **same
  idempotent path** as the webhook (`adapter_jira/ingest.py`). Because the `event_id`s are
  derived from the issue's **state** (issue id + status + comment id —
  never from the webhook's changelog), webhook and poller converge on the same
  `event_id` and whichever path arrives second dedups. **They never duplicate** (proven in
  `tests/test_poller_webhook_idempotency.py`, in both directions).

- **Outbound**:
  - **Per-ticket serialized transitions** (`adapter_jira/transitions.py`,
    `POST /internal/transition` enqueues; `python -m
    adapter_jira.transition_main` drains). Jira Cloud rejects concurrent
    transitions on the same issue; the worker guarantees, via a per-ticket
    advisory lock (`pg_try_advisory_lock(hashtext(ticket_key))`), that only one transition
    per ticket runs at a time — different tickets proceed in parallel.
    Idempotent enqueue by `dedup_key`.
  - **Single status comment** (`POST /internal/status-comment`): the SAME
    `dse_contracts.mutable_comment.MutableCommentWriter` as the
    Slack/GitHub adapters, with a new `JiraCommentBackend` (surface `jira`, same
    `comment_state` table).

## Running locally

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

## Tests

```bash
cd /Users/saraiva/Documents/DSE/fase1/services/adapter-jira && pytest -q
```

Result from this session: **17 passed**. Requires a real Postgres (`localhost:5432`,
migrations `0002_wsa.sql` + `0008_wsa2.sql` applied) — no DB mocks. Jira itself
is 100% fixture (`FakeJiraClient`); the business logic (backend, transition
serialization, poller, ingestion) is the real thing.

## What is a local fixture/mock (not production)

- `FakeJiraClient` (`adapter_jira/backend.py`): in-memory, replaces the
  HTTP transport in the tests. `RealJiraClient` (Jira Cloud REST API v3,
  `requests` + Basic auth with a service account) is what runs in production.
- Secrets (`JIRA_WEBHOOK_SECRET`, `JIRA_BASE_URL`, `JIRA_ACCOUNT_EMAIL`,
  `JIRA_API_TOKEN`): read from Vault (`dse/jira/service_account`) via
  `dse_secrets`, with a fallback to env vars — no real Jira site was
  registered in this session.

## Gaps / what needs real infra (documented, not hidden)

1. **A real Jira Cloud site**: register the service account with a scoped
   (project-level) token, create the dynamic webhook with a secret (for the
   `X-Hub-Signature`), and store `base_url`/`email`/`api_token`/`webhook_secret`
   in Vault under `dse/jira/service_account`. Without that, `RealJiraClient` is not
   exercised end to end.
2. **Approval attribution by the poller**: the poller only sees the issue's current
   state, not the changelog, so it does NOT know WHO made a transition. An approval
   reconstructed by the poller (webhook dropped) is attributed to the system
   principal `system:adapter-jira-poller`; the webhook, when it is not dropped,
   carries the real actor and wins because it arrives first (dedup by `event_id`).
   Tasks do not have this limitation (attributed to the reporter, which is stable).
3. **Transition ordering across workers**: the advisory lock guarantees Jira's hard
   invariant (never concurrent on the same ticket). Strict ordering of
   near-simultaneously enqueued transitions is preserved within a
   worker (by `id`); across distinct workers it is best-effort — in production
   a single transition worker runs by default.
4. **`SIGNAL_PLAN_APPROVAL` handler**: the dispatcher routes to the correct signal
   name (`dse_contracts.SIGNAL_PLAN_APPROVAL`); the corresponding
   `@workflow.signal` is being built by WS-B (WSB-E3-T2) in parallel. Until it exists, a
   routed approval is delivered to Temporal and (with no handler) dropped — with no error.
