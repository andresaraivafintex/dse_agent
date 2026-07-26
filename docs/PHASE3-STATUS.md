# Phase 3 ("Evidence") — Implementation status

Date: 2026-07-20. Scope: real implementation of Phase 3 on top of the integrated Phases 1+2, with
the adjustments from [addendum 02](../../plano-desenvolvimento/02-ADENDO-FASE3-POS-FASE2.md).

## Executive summary

- **~470 tests passing, 2 skipped, 0 failing** (Phase 2 closed at 399; +71 new in Phase 3):
  contracts 14 · WS-A 107 · WS-B 47 · WS-C 78 (65 sandbox + 13 egress) · WS-D 51 · WS-E 103 ·
  WS-F 121 (114 platform + 7 audit). Against Postgres/Temporal/Docker/Vault/LiteLLM **and a
  real Kubernetes cluster (k3d) with Argo CD**.
- **Entry gate satisfied before the build** (the answer to the 14 boundary bugs of Phases
  1-2): session models promoted to `dse_contracts` with `test_activity_boundaries.py`
  validating the literal payloads of the call sites; `RunL2ReviewInput` with `extra="forbid"` (P3 at
  decode time); evidence contracts defined before implementation; k3d cluster + Argo CD.
- **The boundary gate paid off immediately**: Phase 3 added 4 evidence Activities and
  **no boundary bug** appeared during integration (against 9 in Phase 1 and 5 in Phase 2) — the 2
  bugs in this integration were of a different nature (ordering and DSN), not contract drift.
- The worker registers **30 Activities** (8 WS-C + 14 WS-E + 8 WS-B local), with no name collisions.

## What was built (real, per workstream)

| WS | Phase 3 delivered | Real proof |
|---|---|---|
| E | **Garage v1.1.0** artifact store (bucket/tenant, presigned TTL, quarantine wired into WS-F's seam, access log, multipart), **@demo Playwright** video (real webm), **per-PR previews via Argo CD ApplicationSet** on the k3d cluster, visual diff (Pillow), full L3 (reflection + targeted re-runs + CI-repair episodes) | 10MB mp4 round-trip byte-identical; expired URL → denied; preview namespace created → **HTTP 200** → TTL reap; quarantine → 404 before the TTL |
| C | **Playwright in the sandbox base image** (`dse-sandbox-base:wsc3`, 2.35GB, chromium) + the `demos/<work_item_id>/` convention; **second substrate Claude Agent SDK** (real v0.2.124) behind the same interface + parameterized conformance suite | `npx playwright test --grep @demo` via `docker exec` in the rootless sandbox → real webm video |
| D | LiteLLM's **native intra-tier failover** (2nd echo instance, same tier) + extended chaos battery (total outage, 429 quota, non-allowlisted egress) | failover proven by killing the primary container with `docker stop`; negative test that fails CI if a fallback crosses tiers (NFR-07) |
| B | **Evidence debounce (ADR-26)** + iteration caps in the review loop + evidence pipeline wiring (trigger_preview → demo → visual_diff, degraded does not block) + OTel history-size metric | 6 comments in one window → ≤1 refresh (time-skipping); fakes that **decode with the contract's real models** |
| F | **Complete ADR-28**: scheduled secret rotation (zero downtime proven) + **real ESO 2.8.0** on k3d (Secret materializes from Vault, negative scope test); retention by classification; **history alert enabled** | concurrent reader during 5 rotations → zero errors; an out-of-scope ExternalSecret never goes Ready |

## Integration bugs (2 — none contractual, thanks to the entry gate)

1. **Plan gate ordering (WS-B).** The Phase 3 edit to the review loop changed the timing and
   exposed an inversion: the workflow set `status=awaiting_plan_approval` **before** writing the
   durable `plan_approval_gate` projection. An observer (queue board, or WS-A's signal routing
   that fires `SIGNAL_PLAN_APPROVAL` based on the status) could see the state with the gate
   still absent. **Fixed**: write the gate before flipping the status — the projection exists
   by the time the state becomes observable.
2. **Retention job DSN (WS-F) — actually an operational note, not a code bug.** The
   `test_artifact_purge_skipped_without_delete_grant` test failed in my integration harness
   because I exported `DSE_DATABASE_URL` as the **superuser `dse`** (to apply migrations);
   retention connects through that DSN and `current_user=dse` **does** have DELETE, so it purged
   instead of skipping. With the correct DSN (`dse_app`), 16/16 pass. **Load-bearing operational
   note recorded**: the retention job (`dse_platform.retention`) MUST run as `dse_app`, never
   as the database owner — otherwise the structural DELETE protection (the same principle that
   armors `audit_log`) is bypassed. The `platform-jobs` service in compose already uses the app
   DSN; the note must go into WS-F's deploy runbook.

## Phase 3 exit criteria (Section 16) — honestly

| Criterion | Status |
|---|---|
| UC1 with complete video evidence (mp4/webm, presigned URL with TTL) | **Met** — real video recorded by Playwright, published to Garage, URL with TTL; proven by test |
| Backend-only PRs skip preview without blocking | **Met** — deterministic paths-filter (FR-20); `skipped_backend_only` counts as success |
| Evidence links expire by policy | **Met** — Garage denies an expired presigned URL; real test |
| Garage multipart/IAM suite validated against a real workload | **Met** — 10MB mp4, explicit multipart, byte-identical round-trip |
| Per-tenant preview caps + debounce proven by counting | **Met** — counting test (WS-E) + time-skipping debounce (WS-B) |

## What is missing (not hidden)

- **Real GitHub App**: previews, L3 targeted re-runs and the `@demo` video against a **real** PR's
  preview still run on `FakeGitHubClient` (the logic is real against the API; the registered App
  is what is missing). It is the same administrative blocker as Phases 1-2 — and Phase 3 is where
  it hurts most (a per-PR preview against a fake repo has limited value). **Kick it off now.**
- **@demo runs on the host, not in WS-C's sandbox in the integrated flow**: WS-C proved execution
  INSIDE the sandbox; WS-B's pipeline still runs the evidence Activity on the worker. Joining the
  two (running `@demo` inside the provisioned sandbox, against the preview URL) is pending
  fine-grained integration.
- **The preview URL is in-cluster** (probed via port-forward/NodePort); exposing it to an external
  reviewer requires a real ingress in the customer's cluster.
- **The TTL reaper** is a Python GitOps job (documented decision — kube-janitor would fight Argo
  CD's selfHeal); the `janitor/ttl` annotation is already written as an upgrade path.
- Substrate with **real inference** (Claude Agent SDK / OpenHands): construction, wiring and
  selection proven; a turn with a real model requires a paid gateway + provider (same limitation
  as since Phase 1).

## How to run

```
cd fase1
make up && make migrate
./infra/k8s-local/setup-k3d-argocd.sh   # cluster + Argo CD (idempotent)
./infra/k8s-local/setup-eso.sh          # External Secrets Operator (WS-F)
# tests: venv activated; platform/validation with the dse_app DSN (NOT superuser — see bug 2):
#   DSE_DATABASE_URL=postgresql://dse_app:dse_app_dev_only@localhost:5432/dse \
#     bash -c 'source .venv-wsf/bin/activate && cd services/platform && pytest -q'
```
