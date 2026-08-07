# Overnight run — three features, three repos, one control file

**Read this first, every time, before doing anything.** It is the only shared
memory between loop iterations. If it disagrees with your recollection, it wins.

Goal: André presents tomorrow morning. Three Slack requests must each reach a
merged-ready PR with a review link, and the multi-repo one must open TWO PRs
from ONE message.

## THE RULE FOR EVERY ITERATION

1. Read this file top to bottom.
2. Run `bash .fable/status.sh` — it prints live state from the VPS. Never guess.
3. Do the FIRST unchecked thing in "Next action", and only that.
4. Update this file before finishing: tick what landed, record what you learned.
5. Never start a work item while another is running. One at a time. The box has
   4 vCPU and a second item makes both slower and the timings unreadable.

## Live state (updated by each iteration)

| test | work item | state | PR |
|---|---|---|---|
| 1 — frontend only | `wi_t1-c6b6fb78` | TERMINADO por mim — rc.31, manifesto antigo, nao podia convergir | — |
| 2 — backend only | `wi_t2-53ea7c73` | **Tester PASSOU**, L1 escalou `manifest_not_configured` — ver secao OPEN | — |
| 3 — ambos os repos | `wi_t3-607800b8` | roteador acertou OS DOIS repos, workflow FAILED logo apos | — |

**rc.32 em voo** (cadeia `bash .fable/ship.sh 60 32`): espera CI da #60 -> merge
-> tag -> imagem -> pin -> VPS. Leva os tres consertos de relato do L1.

O gate `tests (validation)` da #60 passou. Os dois vermelhos de 15m01s eram
`cancelled` por `timeout-minutes: 15` durante um incidente do GitHub Actions
("Failed to resolve action download info"), nao falha de teste — historicamente
esses jobs levam 1 e 6 minutos.

## Next action (do the first unticked one)

- [x] DONE 04:01 — rc.27 live and verified ON the machine: 12 deployments, 0
      pods out of Running, migrations 0038/0039 applied, `repo_profiles` seeded,
      and `openjdk version "21.0.11"` confirmed INSIDE the agent-runner image
      alongside node v22.
- [x] DONE 04:20 — the whole sequence now runs unattended via
      `bash .fable/run-tests.sh` (background task `b43408k9w`). It waits for
      test 1, then sends test 2, waits, sends test 3, waits — one item at a
      time — and appends every transition to the Log below. It gives up on an
      item at attempt >=4 or a heartbeat older than 500s rather than burning the
      night on a doomed run.
- [x] DONE 04:12 — the Slack channel WAS bound to the frontend repo, so the
      resolver decided deterministically and the router was never consulted.
      Deleted that row. Restore it with:
      `INSERT INTO repo_bindings (tenant_id,platform,binding_type,binding_value,repo,base_branch) VALUES ('fintex-poc','slack','channel','C0BKA7TMMEY','fintexinc/bmo-fee-calculator-fe-dse','main');`
      The Jira project binding (BD -> backend) is untouched.
- [x] DONE 07:35 — three root causes found and fixed, all mine (see the Log).
- [ ] Watch `bzyq6ek2x` (CI #56 -> merge -> rc.28 -> deploy -> the three tests).
      If it aborted, the reason is in the Log. Fix it and re-run
      `bash .fable/run-tests-all.sh` once rc.28 is live.
- [ ] OLD, superseded: watch `b43408k9w`. If it died, re-run `bash .fable/run-tests.sh` — the
      `esperar` step is idempotent for an item already finished, but a re-run
      would send test 2 AGAIN. Check the table above first and comment out the
      steps already done.
- [ ] Test 2's item is BACKEND — it exercises the JDK 21 path for the first
      time. If its L1 fails, read `validation_runs.findings` before assuming the
      manifest is wrong; `-Dmaven.compiler.release=17` and the excluded
      Spring context test are both deliberate and documented in the repo.
- [ ] Verify each PR actually contains the change (files, additions).

## How to send a test

```
ssh dse-vps 'bash -s' <<'R'
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
WI="wi_t2-$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
INPUT=$(python3 -c "
import json; print(json.dumps({
 'work_item_id':'$WI','tenant_id':'fintex-poc','requester':'andre','source':'slack',
 'repo': None, 'base_branch':'main',
 'task_content':'<the sentence>'}))")
sudo k3s kubectl -n dse exec deploy/dse-dse-temporal -- temporal workflow start \
  --address dse-dse-temporal:7233 --namespace default --task-queue dse-core-task-queue \
  --type WorkItemLifecycleWorkflow --workflow-id "$WI" --input "$INPUT" < /dev/null
R
```

**`repo` MUST be None** — that is what makes the router run. Passing a repo
skips the whole feature being demonstrated.

## The three sentences, verbatim

1. `On the reports dashboard, show at a glance whether each report is still in progress or finished — a coloured badge, not just the page name buried in a column.`
2. `Calling the payout-levels API in the deployed container comes back as a 500 even though it works fine when I run the service from my IDE — fix that, and while you're in there let me fetch a single payout level by its id.`
3. `Admins need to retire a payout level instead of deleting it, and retired levels must stop feeding advisor fee calculations.`

## Facts already established — do not re-derive

- A work item takes ~20 min for a code change (Tester ~9 min + L1 ~10 min) and
  ~44 s for a documentation-only one. `run_l1_pipeline` and `run_tester_turn`
  are 84% of the clock. There is NO dead time in the workflow.
- `awaiting_human_review` is SUCCESS, not a hang. The DSE never approves its own
  work. A PR exists at that point.
- The heartbeat, not ledger silence, tells you whether an item is alive. A
  20-minute L1 gate writes nothing to the ledger and is perfectly healthy.
- The backend repo now has `.dse/validation.json` and `AGENTS.md`. Its `test`
  gate excludes `BmoFeeCalculatorBeApplicationTests` (needs a live Postgres) and
  every command passes `-Dmaven.compiler.release=17` — the sandbox runs JDK 21
  and that pom's `compiler-plugin.release` property is declared but never
  referenced, so without the flag javac links against the 21 API.
- Sibling work item ids are HASHED (`sha256(event_id:repo)`), never suffixed:
  `pod_name_for` truncates at 63 and an id is already 67 chars, so suffixes
  collide with certainty.
- Preview: `*.preview.notas.api.br` already resolves to the VPS, Traefik and ACME
  are live, and the feature has produced 8 real URLs before. It is gated off by
  `repo_bindings.deploys_preview = false` for both repos, and `mode: source`
  clones UNAUTHENTICATED so private repos fail. A usable preview also needs an
  Auth0 callback URL that only André can add. Do not sink the night into this
  until the three tests are done.

## The two-day loop, finally diagnosed (rc.32) — READ THIS FIRST

Every gate's VERDICT was correct the whole time. Every gate's EVIDENCE
described something else. Nothing was ever wrong with the Coder.

- **`_tail(result.stdout or result.stderr)` — the `or` drops stderr** whenever
  stdout is non-empty, which for a Node toolchain is always. Measured on one
  `npx jest --ci`: stdout 24,610 lines (console noise + coverage table), stderr
  7,074 (the FAIL headers and the counts). `detail` was the coverage table every
  time. **No value of `_MAX_DETAIL_LINES` could ever have fixed it.**
- **The summary regex was pytest-ordered.** Jest writes `2 failed, 275 passed`;
  the pattern wanted `passed` first with `failed` optional after. So a run with
  two broken suites published the same string as a green one: `summary: 275
  passed`. That is the string `audit_log` keeps and the string
  `_l1_failure_context` hands the next Coder turn.
- **`detail` never carried the counted lines.** `error_lines` was computed,
  filtered, used for the verdict, discarded. The operator saw the alphabetical
  tail of a 262-error dump with 24,569 lines omitted.
- **Consequence, from the ledger:** `wi_t1-f0a824a0` ran four L1 rounds with a
  BYTE-IDENTICAL diff. The Coder never moved. Raising `coder_retry_cap` buys
  more rounds of a loop with a measured delta of zero — do not raise it.

Measured ground truth on an untouched checkout of the FE repo's `main`:

| command | exit | result |
|---|---|---|
| `npx tsc --noEmit` (what the manifest ran) | 2 | 262 errors |
| `npx tsc --noEmit -p tsconfig.app.json` | 0 | 0 errors |
| `npx tsc --noEmit -p tsconfig.dse.json` (new) | 0 | 0 errors, 2.9s, 426 files |
| `npx jest --ci --passWithNoTests` | 0 | 275 suites, 4975 tests |

So the test command was ALREADY honest and passable; only typecheck was broken,
and it was broken in the repo's manifest, not in the platform. Fixed on the FE
repo's `main` (commits `db8435a`, `b14edc8`). The two suites that failed in
`wi_t1-c6b6fb78` were the Tester's OWN specs — a missing `TestBed` provider.

**Do not build baseline comparison.** `_only_in_changed_files` already delivers
its value for lint/typecheck, and for `test` the base suite is measurably green.

**Next free win, not yet done:** L1's `test` is 410s of which ~85% is a cold
ts-jest transform cache (214s cold vs 36s warm, coverage on either way). The
Tester ran the identical suite in the same Pod one activity earlier. Find out
why the cache is cold between the two and persist it — worth ~6 min per round
with no weakening of the gate.

## OPEN — the backend's manifest is invisible to L1, and I do not yet know why

`wi_t2-53ea7c73` (rc.31, 15:45 UTC): the router decided backend-only correctly,
the planner ran, the **Tester PASSED with a real Java test**, and then L1
escalated `l1_manifest_not_configured`.

What is RULED OUT by measurement, do not re-check:

- The manifest is on the BE repo's `main` since 03:08 UTC — 12 h before the run.
- `main` is the repo's ONLY branch and its default. No branch mismatch.
- `sandbox_provisioned` says `reused_existing: false`. Not a stale workspace.
- `.git/info/exclude` only ever excludes `.dse-task-branch`, never `.dse/`.
- The BE `.gitignore` does not mention `.dse`.
- `repo_clone.py:91` is a real `git clone --branch <base> --depth 50`, so the
  clone's tree DOES carry `.dse/validation.json`.

What the evidence says: L1 reported NOT_CONFIGURED, not ERROR. Per
`config.py:515-531` that means `git cat-file -e <base_sha>^{commit}` SUCCEEDED
and `git show <base_sha>:.dse/validation.json` FAILED — the commit exists in the
sandbox but its tree lacks the file. And `823b03b2…`, the base_sha, does NOT
exist on GitHub, so `checkpoint(phase="base")` found the tree dirty right after
provisioning and made a commit (`git_checkpoint.py:55-62`). The FE's base_sha
`b686166` IS a real GitHub commit, so the FE tree was clean and the FE path
never exercised this.

**Settle it with a live pod, not by reading.** Start a BE item and, while it is
still provisioning, run inside `dse-sbx-<id>`:

    ls -la /workspace/.dse /workspace/repo/.dse 2>&1
    cd /workspace && git status --short | head && git log --oneline -3
    git show $(git rev-parse HEAD):.dse/validation.json | head -3

That names the cause in one shot. Do not guess at it again — two hypotheses
have already been killed by measurement.

## Facts learned the hard way overnight

- **The Tester could not run a Maven suite.** Detection was
  `package.json -> npm, else pytest`. A Java repo has neither, so it ran pytest,
  which finds nothing and exits non-zero — reported as "your tests fail". Both
  backend items died at `tester_retry_cap_exhausted` without running one Java
  test. Fixed in #56: `pom.xml` -> `./mvnw test`, pytest last.
- **`repo_bindings` is not "the tenant's repositories".** It has one row per
  BINDING, so deleting the Slack channel row took the frontend out of the
  router's candidate set entirely — it then answered "the tenant has a single
  repository" and sent everything to the backend. The candidate set is now
  `repo_bindings UNION repo_profiles`. Fixed in #56.
- **The Angular repo has TWO test trees.** `src/**/*.spec.ts` is Jest;
  `tests/` is Playwright. The Tester wrote unit tests into `tests/`, which
  `npm test` never runs, and the suite failed for a reason unrelated to the
  code. Its AGENTS.md now says so (pushed to the FE repo directly).
- **A work item started via `temporal workflow start` has NO `work_items` row**,
  so any wait keyed on `work_items.status` hangs forever. Wait on the WORKFLOW
  status instead. This cost 3 hours of the night.

## Log — append, never rewrite

- 04:05 — control file created. rc.27 chain in flight.
- 04:12 — deleted the Slack channel binding; the router can now run. Test 1 was
  admitted BEFORE this, so it was routed by the binding, not by the router —
  its outcome is still valid but it does not demonstrate routing. Re-run it at
  the end if there is time.
- 04:12 — `repo_profiles` already has both rows; migrations 0038/0039 applied.

## If an item stalls at `awaiting_repo_selection`

That means the router returned nothing and the workflow fell back to asking a
human — who is asleep. Do NOT wait. Read the `repo_routing_decided` audit row
for the reason, fix it if it is ours (gateway unreachable, model name wrong),
and start a fresh item. If it is not fixable quickly, start the item with an
explicit `repo` so the feature still lands, and record here that this one did
not exercise routing.
- 04:03 — sequencia iniciada
- 04:20 — teste1 terminou em 'failed' — investigar
- 04:21 — teste2 disparado: wi_t2-3542065d
- 05:53 — teste2 nao terminou em 90 min
- 05:54 — teste3 disparado: wi_t3-35fc7db3
- 07:26 — teste3 nao terminou em 90 min
- 07:26 — sequencia concluida
- 07:32 — rc.28: espera CI da #56
- 07:36 — rc.28: merge da #56
- 07:36 — rc.28: build 31081595074
- 07:39 — rc.28: pin e deploy
- 07:42 — rc.28 NO AR — iniciando os tres testes
- 07:42 — sequencia iniciada
- 07:42 — teste1 disparado: wi_t1-537ecb7c
- 09:15 — teste1 nao terminou em 90 min
- 09:15 — teste2 disparado: wi_t2-b52823fb
- 12:03 — rc.29: espera CI da #56
- 12:03 — rc.29: merge da #56
- 12:03 — rc.29: build 31099815525
- 12:05 — rc.29: pin e deploy
- 12:08 — rc.29 NO AR — iniciando os tres testes
- 12:08 — sequencia iniciada
- 12:08 — teste1 disparado: wi_t1-41aba6f8
- 12:50 — teste1: workflow TERMINATED — investigar
- 12:50 — teste2 disparado: wi_t2-c4412436
- 13:00 — teste2: workflow COMPLETED
- 13:00 — teste3 disparado: wi_t3-ce2e2baa
- 13:01 — teste3: workflow FAILED — investigar
- 13:01 — sequencia concluida
- 13:03 — rc.30: espera CI da #56
- 13:09 — rc.30: merge da #56
- 13:10 — rc.30: build 31104647719
- 13:13 — rc.30: pin e deploy
- 13:15 — rc.30 NO AR — iniciando os tres testes
- 13:15 — sequencia iniciada
- 13:15 — teste1 disparado: wi_t1-f0a824a0
- 14:42 — rc.31: espera CI da #56
- 14:49 — rc.31: merge da #56
- 14:49 — rc.31: build 31112751018
- 14:51 — rc.31: pin e deploy
- 14:53 — rc.31 NO AR — iniciando os tres testes
- 14:53 — sequencia iniciada
- 14:53 — teste1 disparado: wi_t1-c6b6fb78
- 15:45 — teste1: workflow TERMINATED — investigar
- 15:45 — teste2 disparado: wi_t2-53ea7c73
- 15:48 — teste2: workflow COMPLETED
- 15:48 — teste3 disparado: wi_t3-607800b8
- 15:49 — teste3: workflow FAILED — investigar
- 15:49 — sequencia concluida
- 15:55 — rc.32: verificando CI da #60
- 16:09 — ABORTADO: CI da #60 tem 2 job(s) vermelhos — nao vou mergear em vermelho
- 16:11 — rc.32: verificando CI da #60
