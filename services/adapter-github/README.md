# adapter-github (WS-A)

The full workstream documentation (what is implemented, fixtures, what is
missing for production, requests to the architect) lives in
[`../ingest-gateway/README.md`](../ingest-gateway/README.md) — this file
covers only what is specific to this service.

## Running locally

```bash
source /Users/saraiva/Documents/DSE/fase1/.venv-wsa/bin/activate
pip install -e ../../packages/contracts -e ../../packages/dse_audit -e ../../packages/dse_identity \
            -e ../../services/platform -e ../../services/ingest-gateway -e .
GITHUB_WEBHOOK_SECRET=dev_only_fixture GITHUB_BOT_LOGIN=dse-bot GITHUB_TASK_LABEL=dse \
  DSE_DATABASE_URL=postgresql://dse_app:dse_app_dev_only@localhost:5432/dse \
  uvicorn adapter_github.app:app --port 8802
```

Endpoints:
- `POST /github/webhook` — GitHub App webhooks (`issues`,
  `issue_comment`, `pull_request_review_comment`, and **Phase 2**
  `pull_request` closed/merged → `merged_by_human` signal, WSA-E4-T3).
- `POST /internal/status-comment` — outbound, under the GitHub App identity.
- `POST /internal/reconcile` — recovery of clarification replies whose webhook
  delivery was lost (see below). Returns `{"ok", "checked", "recovered"}`.
- `GET /health`.

## Reply reconciler (`POST /internal/reconcile`)

A lost webhook delivery leaves the task in `needs_clarification` forever,
silently, with the human's answer sitting on the issue — it took a hand-written
UPDATE on the database to unblock, twice in one afternoon. This endpoint re-reads
the threads of the work items blocked waiting on a human reply
(`ingest_gateway.pending_reply_work_items`) and feeds the comments through the
same intake the webhook uses; anything already ingested dedups on `event_id` and
costs nothing, so it is safe to call on a timer.

Two rules it will not bend:
- **replies only.** `awaiting_plan_approval` is excluded. Re-reading is exactly
  what the TOCTOU defense (WSA-E2-T2) forbids for an approval — an attacker can
  post something benign, get it approved and edit it afterwards. A lost approval
  stays lost and a human re-approves.
- **never its own voice.** Comments authored by a bot (`user.type == "Bot"`, a
  `[bot]` login, or `GITHUB_BOT_LOGIN`) are skipped, or the DSE would read its
  own clarification question back as the answer.

Every recovered event lands in the audit ledger as `reply_recovered` (actor
`system:adapter-github-reconciler`, with the work item and the comment id), so a
recovered reply is never mistaken for a delivered one.

**Phase 2 (WSA-E4-T3 + WSA-E1-T5):** the `pull_request` merged webhook fires
`merged_by_human`; a PR closed without merging fires nothing. Tenant resolution
uses `installation.id` via `tenant_platform_bindings` (documented fallback to
`DSE_TENANT_ID`). See [`../ingest-gateway/README.md`](../ingest-gateway/README.md#phase-2--what-ws-a-added).

## Tests

```bash
cd /Users/saraiva/Documents/DSE/fase1/services/adapter-github
pytest -q
```

Result from this session: **24 passed** (19 Phase 1 + 5 Phase 2: merge webhook +
tenant binding, `tests/test_merge_and_tenant.py`). Requires a real Postgres
(`localhost:5432`, migration `0002_wsa.sql` applied) — no DB mocks.
GitHub itself is 100% fixture (`FakeGithubClient`) in the outbound tests; real
GitHub App authentication (`adapter_github/auth.py`) is not exercised
in an automated test in this session because there is no real App/private key —
the logic (JWT RS256 + exchange for an installation token) follows GitHub's
official API flow and is ready to use as soon as the real credentials
exist (see `GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY`/
`GITHUB_APP_INSTALLATION_ID`).
