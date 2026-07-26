# Plan — 100% automatic flow: label `dse` → DSE on the VPS → PR

Produced by parallel research (3 streams + synthesis) on 2026-07-24, against the
actual state of the code. Context: the POC on the VPS already proved the engine (gVisor Pod +
in-pod clone + real Claude Coder → commit → PR #22 opened manually). This
plan covers what is left for the flow to be **triggered by a label and reach the PR
on its own**.

## Rebuild decision (what drives the cost)

| Component | New image? | Why |
|---|---|---|
| **agent-runner** | **YES — 1 rebuild (emulated amd64 ~20min OR release rc.3)** | The **Tester** has to run the suite (pytest/npm) + infra-error loop + commit/push **inside the Pod**. Today `--op ∈ {turn, bootstrap, checkpoint, post_turn}` (`agent-runner/agent_runner/__main__.py:26`) — there is no op that executes tests. It needs a new `--op tester`. |
| **orchestrator / sandbox-runtime** | **NO — hot-patch (ConfigMap)** | Planner, Reviewer and the Tester's `pod_git` branch are just worker-side code (editable install). |
| **validation (L1, finalize, update_base_branch)** | **NO — hot-patch** (confirm the overlay) | A new `KubectlExecSandbox` is enough; the sandbox image already ships git+toolchain (the Coder already runs there). |
| **adapter-github + ingest-gateway (webhook)** | **NO — already built and tested (24 tests)** | Deploy + config only. |

**Only the Tester forces the expensive rebuild.** Planner and Reviewer do NOT need a Pod (the
stage analysis had over-scoped the Planner).

## What needs porting to K8s (it is not just the Tester)

After `plan_auto_approved`, the low/medium README flow runs ~14 stages. Already
ported (they route to the K8s driver): `provision_sandbox`, `checkpoint/capture_base_sha`,
`rebuild`, `teardown`, **`run_coder_turn`**. Missing:

| Stage | K8s blocker | Fix | Rebuild? |
|---|---|---|---|
| **run_planner_turn** (`activities.py:832`) | `reject_local_agent_execution("planner")` in prod | relax the gate (it is gateway-first, runs before provision, tolerates having no workspace) | No |
| **run_tester_turn** (`activities.py:1266`) | gate + writes/runs tests/commits on the host (`:1338-1341`, `:1403`) — **non-skippable** (`enforce-tester-result-v1`, `workflows.py:1298`) | `--op tester` in the runner + `pod_git` branch in the Activity | **YES** |
| **run_l1_pipeline** (`validation/activities.py:231`) | `executor_for_handle`→`DockerExecSandbox` (`docker exec` against a pod) | `KubectlExecSandbox` (`kubectl exec`) in `sandbox_exec.py` | No |
| **run_l2_review** (`activities.py:1495`) | `reject_local_agent_execution("reviewer")` | relax the gate (it only uses plan+diff, no workspace) | No |
| **finalize_pr** (`validation/activities.py:252`) | the `git push` goes out via `DockerExecSandbox`; **opening the PR already runs on the control plane (ok)** | push via `KubectlExecSandbox` | No |
| **update_base_branch** (`validation/activities.py:453`) | git in the host workspace (only on the changes_requested path) | git in-pod | No |

## Execution order

- **Phase 0** — confirm `DSE_DEPLOYMENT_PROFILE=production` + `k8s`/`INPROCESS=0`;
  confirm the ConfigMap overlay covers `sandbox_runtime/` **and** `dse_validation/`.
- **A1 Planner** (hot-patch, 0.25d): relax the gate (`activities.py:837`).
- **A2 Reviewer** (hot-patch, 0.25d): relax the gate (`activities.py:1497`); optionally
  wire a real verdict through the gateway (`_model_reviewer_verdict`, following the
  `_model_plan_proposer` pattern).
- **A3 KubectlExecSandbox** (hot-patch, 0.75d): new class in `sandbox_exec.py`
  (`kubectl exec -i <pod> -- argv`, mirroring `k8s_driver._kubectl`); `executor_for_handle`
  returns it on the K8s driver. Unblocks L1 + finalize(push) + update_base_branch **without
  touching the image**. Pass `authenticated_remote_url` as an **argv** of `git push`.
- **A4 Tester** (REBUILD, 2d + build — critical path): `TesterOpRequest/Result` in
  `packages/contracts/dse_contracts/agent_turn.py`; module `agent-runner/agent_runner/tester_op.py`
  (authoring via the gateway **in the Pod** + run_tests + infra-error loop + commit/push); `--op tester`
  in `__main__.py`; vendor `_tester_lib.py` + `model_gateway_client` into the Dockerfile; **amd64
  build**; `pod_git` branch in `_run_tester_turn_impl` (hot-patch, depends on the op existing).
- **Track B Webhook** (deploy/config, 0.5–1d, in parallel): deploy `adapter-github` +
  `ingest-gateway`; register the webhook (`.../github/webhook`, `issues` events, HMAC
  `GITHUB_WEBHOOK_SECRET`); env `GITHUB_TASK_LABEL=dse`; cloudflared tunnel; orchestrator
  egress → `api.github.com` + the App secrets (already in `dse-poc-secrets`).

> Once A1+A2+A3+Track B are done, the flow is triggered by a label and goes green **except for
> the Tester** (which fails at the `enforce-tester-result` gate). The automatic PR only closes
> with **A4**.

## Incremental verification
1. A3: `kubectl exec` in a coder Pod runs lint → `run_l1_pipeline` green.
2. A1/A2: workflow up to `plan_auto_approved` in prod → Planner/L2 with no reject.
3. A4 raw op: `kubectl exec -i <pod> -- python -m agent_runner --op tester` (JSON on stdin) → `TesterOpResult`.
4. A4 Activity: full workflow, `enforce-tester-result` gate green.
5. Track B: `curl` a signed payload at `/github/webhook` → row in `ingest_events` + `start_workflow`.
6. E2E: label `dse` on an issue → workflow → PR.

## Estimate
**~5–6 days.** Critical path = **A4 (Tester + the single emulated build)**.
A1/A2/A3 and Track B run in parallel with A4's development; only the integration of the
Tester Activity (A4's final step) waits on the image. **Do A3 first** (cheapest return,
unblocks L1/finalize without touching the image).

## Known follow-up (out of scope for this plan)
A **real** model-backed Reviewer needs an actual diff, but K8s today returns a
placeholder `diff_summary` (`remote_substrate.py:194-202`) — that would require a diff op
in the Pod (a second rebuild). `_default_reviewer_verdict` (fixture) runs without it.
