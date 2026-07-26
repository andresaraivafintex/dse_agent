# Phase 4 ("Loop hardening & learning") — Implementation status

Date: 2026-07-21. The last ENGINEERING phase before the pilot gates. Scope and adjustments per
[addendum 03](../../plano-desenvolvimento/03-ADENDO-FASE4-POS-FASE3.md), preceded by a deep
validation of the current state.

## Executive summary

- **597 tests passing, 0 failing, 5 skipped** (Phase 3 closed at 503; +94 in Phase 4) — the whole
  suite re-run with the correct per-workstream DSNs, against Postgres/Temporal/Docker/
  Vault/LiteLLM/Garage **and the k3d cluster + Argo CD**.
- **Zero contract integration bugs for the second phase in a row** — the entry gate
  (contracts + boundary tests + `extra="forbid"`) keeps paying off: the 3 new activities
  (`update_base_branch`, `eval_skill_candidate`, `promote_skill`) and the 26 cross-workstream names
  registered with no collision; the worker comes up with **36 activities**.
- **All Phase 4 engineering is delivered and proven.** What separates the product from the pilot
  now is **administrative/business**, not code — see §"Pilot-readiness".

## What was built (real, per workstream)

| WS | Phase 4 delivered | Real proof |
|---|---|---|
| E | **merge-base (new construction)** — updates the task branch with base drift by merging, never rebasing after the 1st review; conflict → escalate; review-feedback episodes | exit test: PR with drift + 2 anchored threads → merge-base → **orphaned_threads==0** (real sha reachability); a negative test proves that rebasing would orphan all of them |
| C | **skill promotion pipeline** candidate→eval→approved→canary→active + pointer-based rollback; episode capture (3 sources) | exit: full pipeline + rollback restores the pointer (the skill disappears from the Planner); **adversarial: promote(active, approver=None) and system:\* refused** before any write |
| A | **steering over the real identity map** (console RBAC + bundle approvers, offboarding overrides), stable Phase 1 signature; **Teams adapter** (provisioned, not activated) | an offboarded user is denied despite the allowlist; swapping the implementation did not break the Phase 1 steering tests; Teams inbound returns 501 until activation |
| F | **threat model + data-flow** (threat→implemented control→test, validated mermaid diagrams); **red-team program** (21 executable attacks); **topology B**; **Webex decision** (formal de-scope with "how to revert") | 21/21 red-team passing against real infra (forged webhook, prompt-injection/SSRF via egress, cross-tenant, malicious skill via WS-C); `helm lint`/`template` clean on A and B |
| B | **merge-base wiring** in the review loop (conflict/orphans → escalate), clarification episode, 4 OTel PR-quality metrics | conflict → `_EscalateNow`; orphaned_threads>0 also escalates (extra defense of the invariant) |

## Phase 4 engineering exit criteria (Section 16) — met

| Criterion | Status |
|---|---|
| UC4 green including the zero-orphaned-review-threads assertion | **Met** — merge-base proven with orphaned_threads==0 + wiring that escalates if >0 |
| First skill promoted candidate→eval→approval→canary with rollback demonstrated | **Met** — full pipeline tested against real Postgres; pointer-based rollback |
| Webex decision executed (restore or de-scope with sign-off) | **Met** — formal de-scope documented (ADR-25), with a mechanical reversion path |
| Threat model + data-flow diagrams (security review package) | **Met** — THREAT-MODEL.md with threat→control→test traceability |
| Red-team program before the first customer repo | **Met** — owner, cadence, 21 automated attacks + manual items |

## Pilot-readiness — the honest boundary (addendum 03 §3)

Phase 4 closes **everything that is engineering**. The remaining pilot gates **cannot be solved
with code** — they are administrative/business and should become a separate readiness checklist:

| Pilot gate (Section 16) | Nature | Blocker |
|---|---|---|
| PR quality thresholds in the internal pilot | Engineering **ready**, data **pending** | The 4 OTel metrics exist and emit; the real NUMBERS require operating against real repos |
| Economics measured (Section 15, real numbers) | same | Cost attribution instrumented (Phase 2/3); real numbers depend on a real model/usage |
| Client security/data review passed | **Ready to submit** | THREAT-MODEL.md + data-flow diagrams ready; the approval is the customer's |
| Signed licensing BOM | Administrative | `infra/OSS-BOM.md` exists; the signature is a process |
| Operational RACI; contractual terms executed | Business/legal | Outside engineering |
| Queue board demonstrably the system of record | **Met** (Phase 2) | — |

**Critical path to the pilot (it is not code):** register **real GitHub App / Slack / Jira /
AWS-Bedrock account**. That is the longest lead time pending since Phase 1 and it now directly
gates "PR quality thresholds" and "economics measured". **Recommendation: kick it off now** — no
line of code unblocks it, and every other piece of engineering is already ready to consume it.

## Declared engineering gaps (honest)

- **Real credentials/services** (GitHub App, Slack, Jira, AWS/Bedrock, a real model): everything
  runs on a clearly marked fixture/fake; the logic is real against the real APIs. Same blocker as
  since Phase 1 — administrative.
- **merge-base**: the core runs against real git in the tests; the Activity wrapper resolves
  anchored threads via the GitHub client (Fake, no App). It joins WS-C's sandbox workspace in the
  final integration with a real repo.
- **canary = shadow** (no traffic-subset selection): documented; real canary selection is a
  post-pilot evolution.
- The skill **eval matcher** is by `pattern_key` (deterministic, auditable, simple) — a rich
  semantic matcher is future work.
- **Teams**: fully provisioned and tested, **not activated** (roadmap decision; Webex de-scoped).

## How to run

```
cd fase1
make up && make migrate
./infra/k8s-local/setup-k3d-argocd.sh && ./infra/k8s-local/setup-eso.sh
# per-workstream tests, venv ACTIVATED; platform/audit with the dse_app DSN (never superuser)
```
