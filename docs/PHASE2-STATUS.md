# Phase 2 ("Judgment & queue") — Implementation status

Date: 2026-07-20. Scope: real implementation of Phase 2 on top of the already-integrated Phase 1,
with the 7 items from [addendum 01](../../plano-desenvolvimento/01-ADENDO-FASE2-POS-FASE1.md) folded in.

## Executive summary

- **399 tests passing, 2 skipped (with a reason), 0 failing** across the 11 packages/services,
  against real Postgres/Temporal/Docker/Vault/LiteLLM. (Phase 1 closed at 223; +176 new in Phase 2.)
- **Temporal upgraded 1.24 → 1.29** (drill WSB-E1-T5), with state preserved (Temporal's own
  databases) — validated by recreating the container. Native Priority & Fairness (1.31+) is not yet
  published in the registry, so fairness is **worker-side** (per-tenant caps) behind a swappable
  interface, exactly as the addendum anticipated.
- **Real end-to-end smoke test** of the Phase 2 path executed: intake → clarification → budget
  → **Planner session** → **plan approval gate** (auto-approved by low risk, with the projection in
  `plan_approval_gate`) → sandbox provisioned. The worker registers **20 Activities**
  (8 WS-C + 6 WS-E + 6 WS-B local ones).
- **5 real integration bugs found and fixed** during consolidation — again, no single
  workstream would have caught them alone (lenient fakes hid the real boundary). See §"Findings".

## What was built (real, per workstream)

| WS | Phase 2 delivered | Tests |
|---|---|---|
| A | Complete Jira adapter (webhook + mandatory poller + serialized transitions + transition-as-approval UC5), platform→tenant mapping, `pull_request` merged webhook → `merged_by_human`, signal routing by WorkItem status | 107 |
| B | Plan approval gate by risk class (with deterministic risk classification that does not downgrade for an optimistic Planner), rejection path (re-plan/re-clarify/cancel), budgets at admission and at boundaries, worker-side fairness, Planner→gate→Coder→Tester→L1→L2→PR sequence, fail-closed model/proxy chaos | 37 |
| C | Read-only Planner / Tester (test-paths) / fresh-context Reviewer sessions (P3 by construction), skill registry bootstrap (tenant-scoped), retrieval/index (self-hosted BM25 + TF-IDF, per-tenant isolation, untrusted content) | 55 |
| D | Per-stage/per-tenant policy at call time (hot-reload), budget enforcement (decline-never-truncate), 4-scope gateway kill switch + model reassign, durable cost wired to the OTel collector, Tier-2 eval suite | 39 |
| E | Fresh-context L2 loop (cheapest-first, P5), bounded L2→Coder fix-retries, strict PR mode (a human opens it, via `PrRef.compare_url`) | 71 |
| F | Access bundles per tenant/channel (an empty approver cascade blocks), ADR-22 design + real OIDC SSO + cascading offboarding, multi-tenant isolation suite (active cross-tenant attempts), admin queue board (API + controls→signals + minimal UI on 8890) | 90 |

## Integration findings (the value of wiring the 6 workstreams together)

1. **`awaiting_plan_approval` missing from the foundation enum** — WS-B wrote the state into the
   TEXT column and `constants.py` already referenced it as a WS-A routing trigger, but the
   `WorkItemStatus` enum and `to_public_status` did not have it. Added to the foundation with
   public projection `blocked` (§10.3).
2. **Broken Planner boundary** — WS-B sent `instructions`(list)+`base_branch`; WS-C's
   `RunPlannerTurnInput` required `instruction`(str). Lenient fakes on both sides
   hid it; the real wire crashed with "missing instruction". The Planner model was made
   tolerant (it derives `instruction` from `instructions`).
3. **Broken Tester boundary, in both directions** — the input was missing `instruction`; and WS-C's
   `TesterTurnResult` return value lacked the `diff_summary`/`files_changed` that WS-B decodes into
   `CoderTurnResult`. Made a compatible superset.
4. **Broken L2 boundary** — WS-B sent `diff_summary`; WS-C's strict input required `diff`.
   Here the fix went into the **caller** (WS-B sends `diff`), deliberately keeping the L2 input
   strict — because the "by construction" P3 proof (the Reviewer's input is exactly
   `{plan, diff}`, no channel for Coder history) is the crown jewel and cannot be
   widened. The P3 tests on both sides were adjusted to the correct payload.
5. **Potential Activity name collision** — WS-C and WS-E both had an "L2 review" concept.
   Verified that WS-E prefixed theirs (`wse_*`) and there is no collision: 14 cross-workstream
   Activities with unique names, worker comes up with 20 registered.

> Recorded technical debt: the Activities' input/output models (Planner/Tester/L2)
> live in each workstream, not in `dse_contracts` — that is what allowed the drift in findings 2–4.
> The definitive fix is promoting them to the foundation (a single source of truth), scheduled for
> the architect's next contract window.

## Phase 2 exit criteria (Section 16) — honestly

| Criterion | Status |
|---|---|
| UC2 green (Jira) | **Partial** — adapter complete and tested with FakeJiraClient; still needs a real Jira service account/site in Vault (operational) |
| UC5 green incl. block-on-unresolvable-approver | **Met in logic** — gate + empty cascade→Blocked proven by 7 WS-B integration tests against real Temporal; the `plan_approval` signal seam (dispatcher→handler) verified statically and the auto gate runs live in the smoke test |
| Queue board shows all states + controls with real effect | **Met** — API + minimal UI (8890) + controls→Temporal signals; 4-scope kill switch and model reassign with real effect |
| ADR-22 design closed before exit | **Met** — `infra/ADR-22-identity.md` + real OIDC SSO + cascading offboarding |
| Skill registry bootstrapped + retrieval operational | **Met** — tenant-scoped registry with a human seed; BM25/TF-IDF retrieval with isolation proven by WS-F's cross-tenant suite |

## What is missing (not hidden)

- **High-risk path end-to-end, live**: the auto gate (low risk) runs in the smoke test; the
  `high → awaiting_plan_approval → plan_approval signal → proceeds` path is proven by integration
  tests (real Temporal, WS-B) but was not forced end-to-end through the containerized stack,
  because the fake Planner emits `expected_files: []` (low risk) and there is no real model to
  emit a high-risk plan. It needs a real model or script injection into the Planner.
- Real credentials/instances (Slack/GitHub/Jira Apps, AWS/Bedrock) remain pending — same
  situation as Phase 1; it is an administrative blocker, not an engineering one.
- Promotion of the Activity models to `dse_contracts` (debt from findings 2–4).
- `dse_ingest_dispatcher` and the other containers: the images were rebuilt during integration; a
  clean production redeploy should rebuild everything (that is already what `make up` does).

## How to run

```
cd fase1
make up && make migrate
# tests: each workstream in its own ACTIVATED venv (WS-E's L1 needs ruff/mypy on PATH):
#   source .venv-wse/bin/activate && (cd services/validation && pytest -q)
```
