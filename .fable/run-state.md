# Run state — make a work item finish in a fraction of the time, without weakening it

Goal: cut the end-to-end wall clock of a work item, and keep the gate as strong
as it is today.

| # | Criterion | Status |
|---|---|---|
| 1 | Measured per-activity breakdown from real runs | VERIFIED |
| 2 | Ranked optimisations, each with its robustness argument | in flight (3 scouts) |
| 3 | The safe ones implemented, tested, merged, deployed | pending |
| 4 | A work item measured before/after | pending |

## Verified facts (measured, not inferred)

Work item `wi_rc25-5fe461b9`, Angular repo, 1030 files, 1401 npm packages,
running rc.25 (sandbox cpu=3, mem=3Gi). Durations from Temporal history:

| activity | seconds | share |
|---|---|---|
| `run_l1_pipeline` | 638 | 45% |
| `run_tester_turn` | 555 | 39% |
| `finalize_pr` | 186 | 13% (three failed retries) |
| coder + planner + provision + checkpoint + teardown | <35 | 2% |
| **total** | **1417 (23.6 min)** | |

Two activities are 84% of the clock. Everything else is noise.

Inside L1, from the gate's own output: `npm ci` = **55s** (it was ~2 min before
the CPU limit went from 1 to 3). So ~583s of the 638 is lint + tsc + jest +
build.

Every L1 gate PASSED on this run, including `typecheck`, which reported
"no type errors in the files this change touched (262 elsewhere in the
repository, not this change's)". The gate is not what blocks a PR any more.

`finalize_pr` failed on `git push failed (exit=-1): - Finding files` — the
target repo's `.husky/pre-push` running `ng lint` on our push until the 60s
timeout. Fixed in PR #52.

## Decision ledger
- This file is NOT committed — scaffolding, not the client's code.
- Speed work is ranked by (seconds saved × confidence) ÷ risk. A change that
  saves a minute but can produce a FALSE GREEN is not taken: the gate exists to
  be believed, and an unbelievable fast gate is worth less than a slow one.
- Findings are scoped to the changed files; gates skip entirely for a
  documentation-only change; `sast` probes for Python before running bandit; an
  OOM/abort is infra, not a verdict on the code. All shipped, rc.24/rc.25.
- Sandbox limits: cpu 1 -> 3, mem 1536Mi -> 3Gi. The node has 4 vCPU and ~10 GB
  free; the limit is a ceiling, not a reservation.

## Open items
- The Tester authored a TypeScript spec to assert a markdown file exists. That
  single file made the change non-documentation-only and dragged the whole
  Angular toolchain back into L1 — undoing the skip that had just landed. Rule
  worth having: if the Coder's diff is documentation-only, the Tester has
  nothing to test.
- A DSE git command must not execute code from the customer's repository,
  wherever it is issued. Fixed three times now at three call sites (checkpoint
  commit #46, hygiene checkouts, push #52). Worth making structural.
- `tests (control-plane)` is timing-sensitive under CI contention: the suite
  uses a time-skipping Temporal env whose clock only advances in idle windows.
  Fails on a different test each time. Rerunning with an empty queue works.
