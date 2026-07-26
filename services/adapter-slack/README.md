# adapter-slack (WS-A)

The full workstream documentation (what is implemented, fixtures,
what is missing for production, requests to the architect) lives in
[`../ingest-gateway/README.md`](../ingest-gateway/README.md) — this file
covers only what is specific to this service.

## Running locally

```bash
source /Users/saraiva/Documents/DSE/fase1/.venv-wsa/bin/activate
pip install -e ../../packages/contracts -e ../../packages/dse_audit -e ../../packages/dse_identity \
            -e ../../services/platform -e ../../services/ingest-gateway -e .
SLACK_SIGNING_SECRET=dev_only_fixture SLACK_BOT_TOKEN=xoxb-dev-fixture \
  DSE_DATABASE_URL=postgresql://dse_app:dse_app_dev_only@localhost:5432/dse \
  uvicorn adapter_slack.app:app --port 8801
```

Endpoints:
- `POST /slack/events` — Slack Events API (`app_mention`, `message` in a
  thread).
- `POST /slack/interactions` — Interactivity (`block_actions`, approval
  buttons).
- `POST /internal/status-comment` — outbound, called by the orchestrator
  (WS-B) on every relevant state transition.
- `POST /internal/reconcile` — recovers clarification replies whose webhook
  delivery was lost (re-reads the threads of the work items blocked waiting on
  a human reply and feeds them through the same intake). Never recovers plan
  approvals — see the endpoint docstring. Body optional:
  `{"tenant_id": ..., "limit": ...}`. Answers `{"ok", "checked", "recovered"}`
  and never 5xx, so a scheduled caller can read it the same way as the GitHub
  adapter's reconciler.
- `GET /health`.

## Tests

```bash
cd /Users/saraiva/Documents/DSE/fase1/services/adapter-slack
pytest -q
```

Result from this session: **14 passed**. Requires a real Postgres
(`localhost:5432`, migration `0002_wsa.sql` applied) — no DB mocks.
Slack itself is 100% fixture (`FakeSlackClient`) in the outbound tests; the
inbound tests exercise the real signature/sanitization/correlation
pipeline.
