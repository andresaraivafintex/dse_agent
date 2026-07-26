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

## Rate limiting (`adapter_slack/ratelimit.py`)

`build_real_slack_client` wraps the `WebClient` in `RateLimitedSlackClient`, so
every outbound call (status message, ephemeral notice, reply reconciler) backs
off on a throttle. Slack's shape: HTTP `429` with `Retry-After` in seconds and
`{"ok": false, "error": "ratelimited"}` in the body — both the raising
(`SlackApiError`) and the returning form are handled. Without a `Retry-After`
the wait is exponential backoff with jitter.

**The wait budget belongs to the caller.** `build_real_slack_client` requires a
`deadline` and every call made through the returned client shares it, so one
client per request means one budget per request — not one per call, which bounded
nothing: `/internal/reconcile` sweeps up to 50 threads inside a single HTTP
request. Each endpoint sets its own, against what its own caller will wait for:

| endpoint | budget | bounded by |
| --- | --- | --- |
| `POST /internal/reconcile` | `RECONCILE_BUDGET_S` = 60s | the reply-reconciler CronJob abandons the request at 120s; the sweep also starts no new thread past the deadline |
| `POST /internal/status-comment` | `STATUS_COMMENT_BUDGET_S` = 3s | the orchestrator posts best-effort with an 8s timeout — a longer retry means it gives up before `save_ref`, and the next transition posts a SECOND message |
| `_notify_ephemeral` (interactivity) | 0s — never waits | it runs inside a coroutine, where a blocking retry sleep parks the uvicorn event loop and `/health` with it |

A `Retry-After` is honoured **verbatim or not at all**: a hint that does not fit
in the remaining budget raises `SlackRateLimited` immediately instead of sleeping
a shorter time and asking again, which cannot succeed and only spends more of the
limit. The reconciler treats that exception as "this workspace is throttling" and
stops the sweep rather than walking the remaining items into the same limit.

Only the throttle shapes are retried. `invalid_auth`/`channel_not_found` are
permanent for that call, and a timed-out `chat.postMessage` may already have
posted — retrying either would break "exactly 1 status message per task".

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
