# Plano — fluxo 100% automático: label `dse` → DSE na VPS → PR

Produzido por pesquisa paralela (3 streams + síntese) em 2026-07-24, sobre o
estado real do código. Contexto: o POC na VPS já provou o motor (Pod gVisor +
clone in-pod + Coder Claude real → commit → PR #22 aberto manualmente). Este
plano cobre o que falta para o fluxo ser **disparado por label e chegar ao PR
sozinho**.

## Decisão de rebuild (o que muda o custo)

| Componente | Imagem nova? | Por quê |
|---|---|---|
| **agent-runner** | **SIM — 1 rebuild (amd64 emulado ~20min OU release rc.3)** | O **Tester** precisa rodar a suíte (pytest/npm) + loop de infra-error + commit/push **dentro do Pod**. Hoje `--op ∈ {turn, bootstrap, checkpoint, post_turn}` (`agent-runner/agent_runner/__main__.py:26`) — não há op que execute testes. Precisa de `--op tester` novo. |
| **orchestrator / sandbox-runtime** | **NÃO — hot-patch (ConfigMap)** | Planner, Reviewer e o ramo `pod_git` do Tester são só código no worker (editable install). |
| **validation (L1, finalize, update_base_branch)** | **NÃO — hot-patch** (confirmar overlay) | Basta um `KubectlExecSandbox` novo; a imagem do sandbox já traz git+toolchain (o Coder já roda lá). |
| **adapter-github + ingest-gateway (webhook)** | **NÃO — já construídos e testados (24 testes)** | Só deploy + config. |

**Só o Tester força o rebuild caro.** Planner e Reviewer NÃO precisam de Pod (a
análise de stages tinha sobre-escopado o Planner).

## O que precisa ser portado pro K8s (não é só o Tester)

Depois do `plan_auto_approved`, o fluxo README low/medium roda ~14 stages. Já
portados (roteiam pro driver K8s): `provision_sandbox`, `checkpoint/capture_base_sha`,
`rebuild`, `teardown`, **`run_coder_turn`**. Faltam:

| Stage | Bloqueio no K8s | Fix | Rebuild? |
|---|---|---|---|
| **run_planner_turn** (`activities.py:832`) | `reject_local_agent_execution("planner")` em prod | relaxar o gate (é gateway-first, roda antes do provision, tolera sem workspace) | Não |
| **run_tester_turn** (`activities.py:1266`) | gate + escreve/roda testes/commit no host (`:1338-1341`, `:1403`) — **não-pulável** (`enforce-tester-result-v1`, `workflows.py:1298`) | `--op tester` no runner + ramo `pod_git` na Activity | **SIM** |
| **run_l1_pipeline** (`validation/activities.py:231`) | `executor_for_handle`→`DockerExecSandbox` (`docker exec` num pod) | `KubectlExecSandbox` (`kubectl exec`) em `sandbox_exec.py` | Não |
| **run_l2_review** (`activities.py:1495`) | `reject_local_agent_execution("reviewer")` | relaxar o gate (só usa plan+diff, sem workspace) | Não |
| **finalize_pr** (`validation/activities.py:252`) | o `git push` sai via `DockerExecSandbox`; a **abertura do PR já roda no control-plane (ok)** | push via `KubectlExecSandbox` | Não |
| **update_base_branch** (`validation/activities.py:453`) | git no workspace host (só no path changes_requested) | git in-pod | Não |

## Ordem de execução

- **Fase 0** — confirmar `DSE_DEPLOYMENT_PROFILE=production` + `k8s`/`INPROCESS=0`;
  confirmar que o overlay de ConfigMap cobre `sandbox_runtime/` **e** `dse_validation/`.
- **A1 Planner** (hot-patch, 0.25d): relaxar o gate (`activities.py:837`).
- **A2 Reviewer** (hot-patch, 0.25d): relaxar o gate (`activities.py:1497`); opcional
  plugar veredito real via gateway (`_model_reviewer_verdict`, padrão de `_model_plan_proposer`).
- **A3 KubectlExecSandbox** (hot-patch, 0.75d): nova classe em `sandbox_exec.py`
  (`kubectl exec -i <pod> -- argv`, espelha `k8s_driver._kubectl`); `executor_for_handle`
  retorna ela no driver K8s. Desbloqueia L1 + finalize(push) + update_base_branch **sem
  tocar na imagem**. Passar o `authenticated_remote_url` como **argv** do `git push`.
- **A4 Tester** (REBUILD, 2d + build — caminho crítico): `TesterOpRequest/Result` em
  `packages/contracts/dse_contracts/agent_turn.py`; módulo `agent-runner/agent_runner/tester_op.py`
  (autoria via gateway **no Pod** + run_tests + infra-error loop + commit/push); `--op tester`
  no `__main__.py`; vendorar `_tester_lib.py` + `model_gateway_client` no Dockerfile; **build
  amd64**; ramo `pod_git` no `_run_tester_turn_impl` (hot-patch, depende do op existir).
- **Track B Webhook** (deploy/config, 0.5–1d, paralelo): deploy `adapter-github` +
  `ingest-gateway`; registrar webhook (`.../github/webhook`, eventos `issues`, HMAC
  `GITHUB_WEBHOOK_SECRET`); env `GITHUB_TASK_LABEL=dse`; túnel cloudflared; egress do
  orchestrator → `api.github.com` + secrets do App (já no `dse-poc-secrets`).

> Ao fim de A1+A2+A3+Track B o fluxo dispara por label e vai verde **exceto o Tester**
> (que falha no gate `enforce-tester-result`). O PR automático só fecha com **A4**.

## Verificação incremental
1. A3: `kubectl exec` num Pod coder roda lint → `run_l1_pipeline` verde.
2. A1/A2: workflow até `plan_auto_approved` em prod → Planner/L2 sem reject.
3. A4 op crua: `kubectl exec -i <pod> -- python -m agent_runner --op tester` (JSON no stdin) → `TesterOpResult`.
4. A4 Activity: workflow completo, gate `enforce-tester-result` verde.
5. Track B: `curl` payload assinado em `/github/webhook` → linha em `ingest_events` + `start_workflow`.
6. E2E: label `dse` numa issue → workflow → PR.

## Estimativa
**~5–6 dias.** Caminho crítico = **A4 (Tester + o único build emulado)**.
A1/A2/A3 e Track B correm em paralelo com o desenvolvimento de A4; só a integração
da Activity do Tester (A4 passo final) espera a imagem. **Fazer A3 primeiro** (retorno
mais barato, desbloqueia L1/finalize sem tocar na imagem).

## Follow-up conhecido (fora deste plano)
Reviewer-modelo **real** precisa de um diff de verdade, mas o K8s hoje devolve
`diff_summary` placeholder (`remote_substrate.py:194-202`) — exigiria uma op de diff
no Pod (segundo rebuild). O `_default_reviewer_verdict` (fixture) roda sem isso.
