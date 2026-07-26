# Phase 1 ("Core loop") — Implementation status

Date: 2026-07-10. Scope: real implementation (not just planning) of Phase 1 of the
[master plan](../../plano-desenvolvimento/00-PLANO-MESTRE.md) — 6 workstreams built in
parallel on top of a shared contracts foundation, then integrated, tested and fixed in this
repository (`fase1/`).

## Executive summary

- **223 tests passing, 2 skipped (each with an explicit reason), 0 failing**, covering the 10
  packages/services in the monorepo, run against **real** Postgres/Temporal/Docker (never
  mocked for the durability/idempotency guarantees).
- A **real end-to-end smoke test** (with no manual shortcuts at all) ran successfully:
  Slack/GitHub → transactional outbox → dispatcher → real Temporal workflow → clarification
  gate → correlated answer (Path B) → real Docker sandbox provisioned and isolated. See the
  §"Smoke test" section below.
- **9 real integration bugs were found and fixed** during consolidation — no individual
  workstream would have caught them on its own, because each one only validated its own slice
  against fakes/stubs. See §"Integration findings".
- **2 genuine gaps remain open** (not hidden): the minimum chaos scenarios for the model/proxy
  path, and the real trigger for `merged_by_human`. See §"What is missing".

## What was built (real, testable, running locally)

| Workstream | Services | What actually works |
|---|---|---|
| WS-A | `adapter-slack`, `adapter-github`, `ingest-gateway` | Transactional outbox, dispatcher with `SELECT...FOR UPDATE SKIP LOCKED` proven under real concurrency, the 4 intake defenses (signature, TOCTOU, sanitization, dedup), Path A/B correlation, steering allowlist |
| WS-B | `orchestrator` | Real Temporal worker, complete WorkItem state machine, clarification gate with timers, human review loop with no automatic merge path at all (statically verified), checkpoint/recovery, operator controls, **chaos test that kills a real Temporal worker and proves recovery with no loss or duplication** |
| WS-C | `sandbox-runtime`, `egress-proxy` | Real rootless Docker containers (no docker.sock, no root, isolated network), real default-deny egress proxy, ephemeral credentials, real OpenHands adapter (`openhands-sdk` installed and working) |
| WS-D | `model-gateway` | Real LiteLLM running, deterministic echo model for zero-cost testing, real virtual keys (mint/revoke via the LiteLLM API), OTel cost spans |
| WS-E | `validation` | Real L1 pipeline (lint/typecheck/test/build + `bandit` SAST + secret-scan + diff-budget/forbidden-paths against `PlanArtifact`), idempotent PR finalizer, CI status consumption, workflow resume from a review comment |
| WS-F | `platform` | Audit ledger with audit-driven reconstruction proven, real Vault client, plaintext secret scanner (whole repo clean), validated Helm charts (`helm lint`/`helm template`), platform CI |

## Smoke test (run in this session, no manual shortcuts)

1. The real `admit_work_item()` admits an incomplete task (no acceptance criteria) →
   `work_items`+`ingest_events` written in a single transaction.
2. The dispatcher (real container, running `run_forever`) drains the outbox and calls the real
   `Temporal.start_workflow`.
3. The real `WorkItemLifecycleWorkflow` runs the completeness check and asks for clarification
   (`clarification_requested`, `missing: [acceptance_criteria]`) — audit row written.
4. The real `record_signal_event()` writes a clarification answer correlated to the same
   `work_item_id` (Path B).
5. The dispatcher drains that second event and signals the workflow with the **correct signal
   name and payload format** (fixed in this session — see findings 3–5 below).
6. The workflow receives the answer, marks `clarification_complete`, and calls
   `provision_sandbox` for real — **a real Docker container is created**, isolated on the
   `dse_sandbox_net` network (no internet gateway, it can only reach the `egress-proxy`).
7. `WorkItem.status` reaches `implementing`. At this point the next real step (`git clone` of
   `acme/demo-repo`, a repository that does not exist) is the honest limit of what can be proven
   without a registered GitHub App and a real test repository — an expected stop, not a system
   failure.

Repeated 2x (including after recreating the Temporal container from scratch, to prove that the
dynamicconfig fix — finding 2 — is real and not a fluke) with the same result.

## Integration findings (the real payoff of running everything together)

Each of these only surfaced once the 6 workstreams were wired to each other — every
*individual* workstream test suite was already passing before these fixes:

1. **Docker network conflict** (`dse_sandbox_net`, created at runtime by WS-C, collided with
   WS-C's own compose declaration) — fixed: the network is now referenced as `external: true`.
2. **Temporal was not really coming up** (`DYNAMIC_CONFIG_FILE_PATH` pointed at a file that does
   not exist in the image; a manual `docker cp` "papered over" it only because the container was
   never recreated) — fixed in the foundation to point at the `docker.yaml` that already ships in
   the image; validated by recreating the container from scratch.
3. **WS-C/WS-E Activities were never registered on the real worker**: the WS-B loader looked for
   a module name (`validation.activities`) and an attribute (`ACTIVITIES`) that did not match
   what WS-C (`sandbox_runtime.activities`, with no exported list at all) and WS-E
   (`dse_validation.activities`, exporting `ALL_ACTIVITIES`) actually published — the 8
   cross-workstream Activities were being silently ignored. Fixed in all 3 places; confirmed in
   the real log: *"Worker up. 12 activities registered."*
4. **3 different Temporal signal names for the same concept**: WS-A used
   `"conversation_signal"` (generic), WS-E used `"review_decision"`, and neither matched WS-B's
   actual `@workflow.signal` handlers (`clarification_answer`, `review_comment`,
   `merged_by_human`). Every signal sent through the automatic paths was silently dropped by
   Temporal (a name with no handler is not an error, it just does nothing). Promoted to single
   constants in `dse_contracts.constants` and fixed on both sides.
5. **Incompatible payload format** even after fixing the name: the workflow reads
   `payload["verdict"]`/`["comment"]` (review) and `payload["text"]`/`["acceptance_criteria"]`
   (clarification); WS-A was forwarding the raw `ConversationEvent` and WS-E was sending
   `{"decision":...}` — none of the keys lined up. Fixed with an explicit translation function
   in the dispatcher (`_build_signal_payload`) and in `review_signal.py`.
6. **Clarification heuristic**: even with the right name/format, a free-form clarification
   answer (human text) never filled in `acceptance_criteria` because there is no structured
   extraction step in Phase 1 — the gate re-asked the same question forever. Fixed with a
   documented heuristic (any non-empty answer satisfies the single checklist item that is not
   repo/branch) — **a genuine limitation recorded, not hidden**: a multi-field checklist per
   task class is future work.
7. **Real race condition in `Dispatcher.drain_all()`**: under real concurrency between 2
   dispatchers, a round could see 0 rows purely because the other one held the lock on the last
   available rows at that instant — that does not mean the queue is empty, but the exit
   heuristic stopped right there, leaving rows unprocessed (reproduced: 12 out of 20 processed).
   Mitigated (require 2-3 consecutive empty rounds + backoff); documented as a probabilistic
   heuristic, not a formal guarantee — in production the dispatcher runs via `run_forever`
   (continuous loop), which does not have this problem.
8. **A WS-F test assumed a REST `/health` route** on WS-C's egress-proxy, which is in fact a raw
   HTTP/CONNECT forward proxy (no routes) — fixed to assert the real behavior (clean 400
   rejection of a malformed request).
9. **One hardcoded key** (`api_key: "sk-eco-local-dev-not-a-real-key"`) in the echo model's
   `litellm_config.yaml` (not a real credential, but it broke the convention used by the rest of
   the file) — moved to an `os.environ/...` indirection; WS-F's secret scanner now reports the
   whole repository clean.

## What is missing for Phase 1 to count as "exited" (Section 16, honestly)

| Exit criterion | Status |
|---|---|
| UC1/UC3 green on an internal repo (incl. PR-opened/CI-green gate) | **Partial** — mechanics complete and tested with fakes; still needs a run against a real GitHub App and repo (no credentials available in this session) |
| NFR-01 chaos test (dispatcher/worker mid-flight) | **Met** — both halves (WS-A dispatcher concurrency, WS-B real worker kill) proven |
| Chaos: minimum failure scenarios for the model path + proxy unavailable fail-closed | **Not met** — no test covers gateway unavailable/key expired mid-task, nor egress-proxy down; a real gap, not built for lack of time in this session |
| First audit-driven reconstruction exercise | **Met** — `reconstruct_work_item_history` tested with a complete sequence |
| Working cost attribution | **Partial** — real spans with cost/tokens; aggregation today is in-memory per process, production needs the OTel collector (already hosted by WS-F, only the integration is missing) |
| No secrets in the sandbox / secrets only via a minimal backend | **Met** — proven by test (WS-C) and by repository scanner (WS-F, 0 findings) |

Other known gaps, documented in each service's README:
- **`merged_by_human` is never actually fired** — there is no `pull_request` webhook handler
  (`action=closed`, `merged=true`) in `adapter-github`; the third pause point (waiting for
  merge) works in the workflow but nothing triggers it automatically today.
- No real credentials (Slack App, GitHub App, AWS/Bedrock account) were used — everything runs
  on a clearly flagged fixture fallback (`fixture=True` in the relevant logs/tests).
- Client K8s cluster: Helm charts validated locally (`helm lint`/`helm template`), never applied
  against a real cluster.

## How to run

```
cd fase1
make up        # brings up all infra + the 8 services (images built automatically)
make migrate   # applies migrations/*.sql (idempotent)
# tests: each workstream has its own venv (.venv-wsa .. .venv-wsf, see CONVENTIONS.md)
```

Ports: Temporal UI `:8088`, Vault `:8200`, model-gateway `:4000`, adapters `:8801-8803`,
orchestrator health `:8900`, egress-proxy `:8806`.
