# adapter-teams (WS-A, Phase 4) — PROVISIONED, not activated

Microsoft Teams adapter mirroring `adapter-slack`/`adapter-github`/`adapter-jira`
(those three are the mold). Delivered **complete and tested**, but
deliberately **not activated**: turning Teams on is a business/roadmap decision
(Phase 4+), and activation requires a **foundation** change that this workstream
does not make in this session (4 agents running in parallel — we do not edit
`packages/*` nor migrations 0001-0019).

Reserved port: **8808** (8801 slack, 8802 github, 8803 gateway, 8804 jira are already
taken; 8808 is the next free one in the WS-A block).

## What is implemented (real)

- **Inbound** (`POST /teams/messages`) — a Bot Framework/outgoing-webhook Activity
  normalized into a `ConversationEvent`, passing through the **4 intake defenses**,
  in order:
  1. `verify_teams_signature` (outgoing-webhook HMAC — see below). 401 + audit on failure.
  2. `content_snapshot` frozen from the payload itself (TOCTOU defense) — `events.clean_text`.
  3. the gateway's `sanitize_content` (invisible unicode + secret redaction).
  4. idempotency: deterministic `event_id` (`events.compute_event_id`) →
     dedup in `admit_work_item`/`record_signal_event` via `UNIQUE`.
  Then: `correlate()` decides Path A (new_task) / Path B (signal) / unauthorized
  — exactly the same path as the other adapters.
- **Outbound** (`POST /internal/status-comment`) — **exactly 1 status message
  per WorkItem, edited in place**, via the SAME
  `dse_contracts.mutable_comment.MutableCommentWriter` as the other adapters, with
  `TeamsCommentBackend` (a new backend). It does NOT depend on activation (the surface is just
  the string `"teams"`) — fully functional and already tested.
- **Signature verification** (`ingest_gateway.security.verify_teams_signature`):
  the **Microsoft Teams outgoing webhook** HMAC scheme — the secret is delivered
  Base64-encoded; on every POST, Teams sends `Authorization: HMAC <base64(HMAC_SHA256(
  decoded_secret, raw_body))>`. Constant-time verification over the RAW
  body. (The "full" Bot Framework channel authenticates with a JWT Bearer against
  Microsoft's OpenID metadata — documented as a channel activation step;
  the outgoing-webhook HMAC is the direct analogue of Slack/Jira and is what is covered.)
- **Real outbound transport**: `backend.RealTeamsClient` speaks the Bot Framework
  Connector REST API (AAD client_credentials token → POST/PUT of activities).
  With no real app registration/tenant in this session, `FakeTeamsClient` replaces the
  transport in the tests — `TeamsCommentBackend`'s logic is 100% real.

## What is missing to ACTIVATE (foundation blocker, business decision)

Exactly two additive steps (no change in this service):

1. **Code** — `Platform.teams = "teams"` in the enum in
   `packages/contracts/dse_contracts/conversation_event.py` (1 additive line).
2. **Migration** — apply `activation.sql` (additive relaxation of the
   `work_items.source` and `identity_links.platform` CHECKs to include `'teams'`).

`platform_compat.is_activated()` detects that state at runtime (no hard-coding):
while not activated, `/health` reports `{"activated": false}` and `/teams/messages`
verifies the signature (a real defense) and then returns **501 `teams_not_activated`**
BEFORE any write (avoiding a platform CHECK violation). After
activation, the same endpoint runs the full pipeline (`correlate`/`admit`) with no
further change. Beyond code, activating in production requires a **real Teams tenant**
+ app registration (Azure Bot) + secrets in Vault (`dse/teams/webhook`,
`dse/teams/bot`).

## Running the tests (real infra)

```
cd /Users/saraiva/Documents/DSE/fase1
source .venv-wsa/bin/activate
pip install -e services/adapter-teams
cd services/adapter-teams && pytest -q
```

Coverage: `test_normalization.py` (extraction + the 4 defenses at function level),
`test_outbound.py` (one-message-per-task via FakeTeamsClient + stateless
persistence), `test_inbound_pipeline.py` (HMAC forgery corpus → 401; validly
signed → 501 gated + audit), `test_activation.py` (the activation guard as an
executable test). The Teams HMAC signature also has its own corpus in
`services/ingest-gateway/tests/test_security.py`.
