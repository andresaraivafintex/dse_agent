# services/validation — WS-E (Validação L1/L2 + PR finalizer)

Fintex DSE. Implementa o `services/validation/` descrito em `CONVENTIONS.md`:
pipeline L1 (lint/typecheck/test/build + SAST/secret-scan + diff-budget/
forbidden-paths), PR finalizer idempotente, consumo mínimo de status de CI, e
o handler de resume-por-review-comment (UC4). **Fase 2 ("Judgment & queue")**
adiciona: orquestração do loop L2 de contexto fresco (WSE-E2-T4), loop de
fix-retries bounded L2->Coder (WSE-E2-T5), e o **modo estrito de PR** em que um
humano abre o PR (WSE-E3-T8, desbloqueado pelo `PrRef.compare_url` novo).

> **Fase 2 — resumo do que foi adicionado** (detalhe abaixo em §Fase 2):
> `dse_validation/l2/` (session/l2_review/fix_loop), 3 Activities novas
> (`wse_run_l2_review`, `wse_record_fix_loop`, `wse_adopt_pr`), `strict_mode`
> wired em `finalize_pr_core`, `migrations/0012_wse2.sql`
> (`wse_l2_reviews`, `wse_fix_loops`, `pr_number` nullable + `compare_url` em
> `wse_pr_tracking`). **71 testes passando** (45 Fase 1 + 26 Fase 2), Postgres
> real, nada mockado para durabilidade/idempotência.

Reusa (não reimplementa) os contratos da fundação: `dse_contracts.mutable_comment.MutableCommentWriter`,
`dse_contracts.plan_artifact.PlanArtifact`, `dse_contracts.activities.{SandboxHandle,L1Finding,L1Result,PrRef,CiStatusResult}`,
os nomes `ACTIVITY_RUN_L1_PIPELINE` / `ACTIVITY_FINALIZE_PR` / `ACTIVITY_CONSUME_CI_STATUS`,
`dse_audit.emit`, `dse_identity.resolve_principal`.

## O que está implementado e funcionando (real, testado contra infra real)

### WSE-E1 — L1: gates determinísticos in-sandbox

- `dse_validation/l1/quality_checks.py` — **T1**: `lint_check`, `typecheck_check`,
  `test_check`, `build_check`. Rodam via `SandboxExecutor` (ver abaixo),
  parseiam saída ESTRUTURADA (conta issues de lint, erros de tipo, resumo do
  pytest — não só o exit code) e nunca truncam o output (P6): timeout vira uma
  `L1Finding(passed=False)` explícita, comando ausente também.
- `dse_validation/l1/sast.py` — **T2 (SAST)**: roda `bandit -r <dir> -f json`
  de verdade e normaliza o JSON em `L1Finding` gate por severidade
  (`DSE_L1_SAST_SEVERITY_GATE`, default `MEDIUM`).
- `dse_validation/l1/secret_scan.py` — **T2 (secret-scan)**: scanner próprio
  (regex + entropia de Shannon, stdlib puro, sem dependência externa) que
  roda DENTRO do sandbox via `python3 -c <script>`: AWS access key id,
  tokens GitHub/Slack, cabeçalho de chave privada PEM, e o caso genérico
  "variável com nome de segredo == literal de alta entropia" (com lista de
  placeholders óbvios ignorados: `changeme`, `xxx`, etc).
- `dse_validation/l1/plan_compliance.py` — **T3**: `git diff --numstat
  <base_branch>...HEAD` real dentro do sandbox, comparado contra
  `PlanArtifact.expected_files`, `diff_budget_lines` e `forbidden_paths`.
  Produz exatamente 2 findings (`diff_budget`, `forbidden_paths`), cada um
  citando o campo do plano violado na mensagem quando falha.
- `dse_validation/l1/pipeline.py` — junta os 8 checks num `L1Result` único
  (`work_item_id`, `passed`, `findings`), persiste em `validation_runs`
  (Postgres) e emite 1 linha de audit (`l1_pipeline_run`, P8). Falha em
  qualquer check não impede os outros de rodar (nunca corta no meio) e não
  decide nada sozinha — quem decide "volta pro Coder" é o workflow do WS-B.

### WSE-E3 — PR finalizer determinístico

- `dse_validation/github/pr_finalizer.py` — **T6**: `finalize_pr_core` faz
  push do branch (via `SandboxExecutor` + `git push`, credenciais da GitHub
  App), abre EXATAMENTE 1 PR por `work_item_id` a partir de um template fixo
  (título com `work_item_id` + resumo; corpo com WorkItem ID, risk_class,
  link de evidência de teste, back-link `Closes #<issue>`). **P1**: nenhuma
  parte usa um LLM — título/corpo são um template Python fixo.
  **Idempotência** provada em `tests/test_pr_finalizer_idempotent.py`
  (3 cenários reais contra Postgres: primeira criação, reexecução após
  sucesso, e reexecução simulando "processo morreu entre criar o PR na API e
  persistir o `pr_number`" — os 3 nunca criam um segundo PR).
- `dse_validation/github/comment_backend.py` — **T7**: `GitHubCommentBackend`
  implementa o `CommentBackend` Protocol da fundação; usado com o
  `MutableCommentWriter` já pronto (não reimplementado) + `PostgresCommentStateStore`
  (tabela `wse_comment_refs`, Postgres real) para o tracking comment único do PR.
- **T8 — "modo estrito"**: **IMPLEMENTADO na Fase 2** (o contrato ganhou
  `PrRef.compare_url` + `pr_number` opcional). Ver §Fase 2 → "Modo estrito".

### WSE-E4 — Consumo mínimo de status checks do PR

- `dse_validation/github/ci_status.py` — **T9a**: `consume_ci_status_core` lê
  os check-runs do PR (`GET /repos/{repo}/commits/{ref}/check-runs`) e agrega
  em `pending|green|red` (green só se TUDO concluiu sem falha; red se
  qualquer um falhou; pending enquanto algo ainda roda — nunca "adivinha").
  Persiste em `wse_ci_status` (Postgres) e devolve `CiStatusResult`. Sem
  preview, sem re-run seletivo (Fase 3, fora de escopo). Implementado como
  **poll sob demanda** (a Activity `ACTIVITY_CONSUME_CI_STATUS` é chamada
  pelo workflow do WS-B quando ele quer saber o status atual), não como
  webhook receiver — por isso **não há `docker-compose.wse.yml`**: WS-E não
  expõe nenhum endpoint HTTP nesta fase (porta 8807 reservada e não usada).
  Se o design evoluir para webhook (menor latência de detecção), a Activity
  `consume_ci_status_core` não muda — só se adicionaria um FastAPI receptor
  fino em `services/validation/webhook.py` que chama a mesma função core.

### WSE-E6-T15 — Resume do workflow por comentário de review (núcleo do UC4)

- `dse_validation/review_signal.py` — `interpret_review_decision` traduz um
  `ConversationEvent` (kind=`approval` ou `review_comment` com `review_state`
  formal em `source_ref`) numa decisão (`approved`|`changes_requested`) de
  forma 100% determinística (P1 — nenhum LLM decide isto). `handle_review_event`
  sinaliza o workflow Temporal (`REVIEW_DECISION_SIGNAL_NAME = "review_decision"`)
  usando `workflow_id = work_item_id`, e SÓ sinaliza se `interpret_review_decision`
  não for `None` — um comentário de review comum não tem NENHUM efeito
  colateral (não sinaliza, não cria WorkItem, não cria PR).
  Prova end-to-end em `tests/test_review_signal_e2e.py` contra o **Temporal
  real** (localhost:7233): inicia um workflow probe mínimo, sinaliza via
  `handle_review_event`, confere que o workflow certo (por `workflow_id`)
  resume com a decisão certa; e prova que um comentário "solto" nem tenta
  sinalizar (retorna `False` sem chamar `get_workflow_handle`/`signal`) e não
  altera `work_items`/`wse_pr_tracking` no Postgres real.

### Integração com o Worker do WS-B

- `dse_validation/activities.py` — `@activity.defn` para as 3 Activities do
  contrato (`ACTIVITY_RUN_L1_PIPELINE`, `ACTIVITY_FINALIZE_PR`,
  `ACTIVITY_CONSUME_CI_STATUS`), expostas em `ALL_ACTIVITIES` para o Worker
  único de `services/orchestrator/worker.py` importar e registrar. Cada
  Activity recebe 1 modelo pydantic de input (`RunL1PipelineInput`,
  `FinalizePrInput`, `ConsumeCiStatusInput`) — ver esse arquivo para a forma
  exata esperada; como o Worker real do WS-B ainda está em desenvolvimento em
  paralelo, esta é a interface proposta por WS-E, sujeita a ajuste de nomes
  de campo na integração final.

## Fase 2 ("Judgment & queue") — o que WS-E adicionou

### WSE-E2-T4 — Orquestração do loop L2 (contexto fresco)

A **sessão** Reviewer L2 (a chamada de modelo de contexto fresco) é construída
pelo **WS-C** (WSC-E3-T5) e exposta como a Activity `ACTIVITY_RUN_L2_REVIEW`
(nome no contrato). O que **WS-E** é dono é a **orquestração** em torno dela:

- `dse_validation/l2/session.py` — `L2ReviewInput` (P3 estrutural: os únicos
  campos são `work_item_id`, `tenant_id`, `plan`, `diff`, `iteration` — **não há
  campo de histórico/transcript do Coder**, então é impossível vazá-lo pela
  fronteira L2), o `Protocol` `L2ReviewSession`, um `FakeL2ReviewSession`
  determinístico (scriptável, sem LLM — usado nos testes) e `build_l2_session()`
  que resolve a sessão real do WS-C (`dse_sandbox_runtime.l2`) se importável ou
  cai no fake com WARNING (nunca falha em silêncio — P8).
- `dse_validation/l2/l2_review.py` — `run_l2_review(...)` roda 1 turno L2, grava
  o veredito + custo em `wse_l2_reviews` (evidência, P8) e emite audit
  (`l2_review_run`). `guard_l2_after_l1(l1_result)` impõe **cheapest-first (P5)**:
  L2 (modelo, caro) só roda depois do L1 (determinístico, barato) verde e antes
  do CI (L3) — tentar antes levanta `L2PreconditionError` (falha limpa, P6).
- **Ordem no fluxo** (o workflow do WS-B chama): L1 -> (verde) -> L2 -> (aprovado)
  -> CI. O gate P1 continua: nenhum LLM decide fluxo; a sessão L2 só **produz**
  um `L2Verdict` estruturado, e a decisão do que fazer é o `fix_loop` abaixo.

> **Fake vs. produção**: como o WS-C constrói a sessão em paralelo, os testes
> usam `FakeL2ReviewSession` (só a chamada de modelo é fake; a orquestração —
> recording, custo, guard, P3 — é a de produção). Assim que o WS-C publicar
> `dse_sandbox_runtime.l2.build_review_session`, `build_l2_session()` passa a
> resolvê-la sem mudar assinatura. Integração alternativa: o WS-B pode chamar a
> Activity `ACTIVITY_RUN_L2_REVIEW` do WS-C diretamente e então a Activity
> `wse_run_l2_review` (WS-E) só grava — hoje `wse_run_l2_review` faz as duas
> coisas via `build_l2_session()` para haver um caminho ponta-a-ponta testável.

### WSE-E2-T5 — Loop de fix-retries bounded L2->Coder

`dse_validation/l2/fix_loop.py` — lógica **100% determinística** (P1) que o
workflow do WS-B consulta a cada veredito L2 (o WS-B é dono da orquestração de
estados; WS-E fornece a decisão):

- `decide_next_action(verdict, state, cfg)` (pura, sem I/O): L2 aprova ->
  `proceed` (segue p/ CI); L2 reprova e ainda há retries **e** budget ->
  `retry_coder` (carrega as objeções específicas de volta ao Coder); retries
  esgotados -> `escalate_operator`; budget esgotado -> `escalate_operator`
  **mesmo com iterações sobrando (P6)**.
- `register_retry(state, coder_cost_usd, l2_cost_usd, cfg)` — **debita budget**
  e incrementa o contador durável (`wse_fix_loops`), audita `l2_fix_retry`.
  Guard P6 belt-and-suspenders: **recusa** iniciar iteração se o cap de
  iterações OU de custo já foi atingido (`FixLoopBudgetExceeded`).
- `escalate_to_operator(state, reason, objections)` — marca o loop exausto
  (durável), audita `l2_fix_loop_exhausted`. Idempotente.
- Config (`config.L2Config`): `DSE_L2_MAX_FIX_RETRIES` (default 3),
  `DSE_L2_BUDGET_CAP_USD` (default 0 = sem teto de custo, só o de iterações).

O contador é durável em Postgres para sobreviver a crash/replay do workflow; o
teste `test_full_bounded_loop_reject_reject_reject_escalate` exercita o ciclo
completo (3 reprovações -> escala; a 4ª iteração nunca ocorre — P6) contra
Postgres real.

### WSE-E3-T8 — Modo estrito: humano abre o PR

`finalize_pr_core(..., strict_mode=True)` (agora wired — o contrato ganhou
`PrRef.compare_url` e `pr_number` opcional). Em vez de abrir o PR, o finalizer:

1. faz **push** do branch (mesma identidade da GitHub App);
2. retorna um `PrRef` com `compare_url` preenchido e `pr_number=None`
   (`compare_url_for(repo, base, branch)` = `.../compare/base...branch?expand=1`);
3. persiste tracking com `pr_number NULL` + `compare_url`
   (`wse_pr_tracking`, colunas alteradas em `0012_wse2.sql`);
4. posta o compare link no **tracking comment único** (via o `MutableCommentWriter`
   da fundação + `GitHubCommentBackend`, `surface="github_pr"`) quando
   `surface_ref` é fornecido;
5. audita `pr_compare_link_posted` (P8).

Um humano abre o PR com 1 clique. Quando o workflow detecta o PR aberto (webhook/
signal do WS-A), chama `adopt_pr_core(...)` (Activity `wse_adopt_pr`): correlaciona
pelo branch/WorkItem e **adota** o PR — preenche `pr_number` na **mesma linha** de
tracking (mesmo WorkItem), audita `pr_adopted`. Idempotente (só o primeiro humano
que abre vence; reexecuções não sobrescrevem). Reexecutar `finalize_pr_core` no
modo estrito também detecta e adota um PR aberto no meio-tempo.

**Flag por repo/tenant** (`config.StrictModeConfig.is_strict_for(tenant, repo)`,
mais específico ganha): `DSE_WSE_STRICT_MODE_TENANT_<T>_<REPO>` >
`DSE_WSE_STRICT_MODE_TENANT_<T>` > `DSE_WSE_STRICT_MODE_REPOS` (lista `tenant:repo`)
> `DSE_WSE_STRICT_MODE` (global). Quando o WS-F publicar a flag em `tenant_config`,
troca-se só `is_strict_for` para ler de lá — a assinatura não muda.

### Activities novas registradas (Worker único do WS-B)

`dse_validation/activities.py` expõe em `ALL_ACTIVITIES` (além das 3 da Fase 1):

| Nome | Input | Retorno | Papel |
|---|---|---|---|
| `wse_run_l2_review` | `RunL2ReviewInput` (só plan+diff, P3; + `l1_passed` p/ guard P5) | `L2Verdict` | orquestra a sessão L2 + grava evidência/custo |
| `wse_record_fix_loop` | `RecordFixLoopInput` | `dict` (estado) | espelha o contador durável do loop (WS-B é dono do estado) |
| `wse_adopt_pr` | `AdoptPrInput` | `PrRef | None` | adota o PR aberto por humano (modo estrito) |

`ACTIVITY_RUN_L2_REVIEW` (contrato) **não** é registrada por WS-E — é a **sessão**,
dona do WS-C. Os nomes de WS-E têm prefixo `wse_` para não colidirem no Worker único.
`FinalizePrInput` ganhou `strict_mode` (opcional; se `None`, resolve via
`StrictModeConfig`) e `surface_ref` (onde postar o compare link).

## Sandbox execution — `SandboxHandle` vs `SandboxExecutor`

`dse_contracts.activities.SandboxHandle` (dono: WS-C) só carrega dados do
handle — não define como RODAR um comando dentro do sandbox. Como
`services/sandbox-runtime` (WS-C) está sendo construído em paralelo e pode não
publicar sua própria interface de execução a tempo, `dse_validation/sandbox_exec.py`
define:

- `SandboxExecutor` (Protocol) — `run(argv, cwd=None, timeout=300) -> ExecResult`.
- `DockerExecSandbox` — implementação REAL via `docker exec <container_id> ...`.
  Funciona assim que `SandboxHandle.container_id` estiver populado pelo WS-C —
  não depende de mais nada do runtime dele.
- `LocalFakeSandbox` — roda o mesmo comando via `subprocess` num diretório
  local (sem Docker). **Usado em TODOS os testes deste workstream** para
  provar a lógica do pipeline L1 (parsing de findings, diff-budget,
  forbidden-paths, SAST, secret-scan) com execuções REAIS de
  bandit/ruff/mypy/pytest/git — só o isolamento de container é substituído,
  nunca a ferramenta em si.

Se WS-C publicar uma interface de execução mais rica, troque só
`DockerExecSandbox` — `SandboxExecutor` e o pipeline L1 que o consome não mudam.

## Cross-workstream: quem é a fonte de verdade para review comments

WS-A (`services/adapter-github`) e WS-E ambos lidam com comentários de GitHub.
Decisão deste workstream: **WS-A é a fonte de verdade para CORRELAÇÃO**
(qual `work_item_id`/PR um `ConversationEvent` pertence, deduplicação de
webhook, verificação de assinatura) e decide genericamente `new_task` vs
`signal` a partir do `EventKind`. **WS-E só INTERPRETA O CONTEÚDO** do sinal já
entregue e correlacionado — ou seja, `dse_validation.review_signal.interpret_review_decision`
assume que `event.source_ref` já contém o suficiente (`repo`, `pr_number`,
`review_state` quando aplicável) e que o `work_item_id` já foi resolvido pelo
WS-A antes de chegar aqui. Se a integração real mostrar que o `review_state`
não é isso que o WS-A anexa em `source_ref`, é só um ajuste de 1 função
(`interpret_review_decision`), não de arquitetura.

De forma simétrica, o backend de comentário GitHub para o tracking comment do
PR (`GitHubCommentBackend`, WSE-E3-T7) é uma implementação PRÓPRIA do
`CommentBackend` Protocol — se `services/adapter-github` já tiver publicado um
backend equivalente quando este código for integrado, o ideal é WS-E passar a
importar aquele em vez de manter uma segunda implementação HTTP da mesma API
(comentário de issue/PR). Ver `dse_validation/github/comment_backend.py`.

## Fixture / modo local vs. o que falta para produção

| Componente | Modo local/fixture (esta sessão) | O que falta para produção |
|---|---|---|
| GitHub App (push, criar PR, comentários, check-runs) | `FakeGitHubClient` (in-memory, mesma interface `GitHubClient`) usado em todos os testes — sem rede/credenciais reais | `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` (PEM), `GITHUB_APP_INSTALLATION_ID` reais de uma GitHub App registrada com permissões `contents:write`, `pull_requests:write`, `issues:write`, `checks:read`. Com essas 3 env vars presentes, `build_github_client()` já usa `RealGitHubClient` (implementado de verdade contra `https://api.github.com`, JWT RS256 via `PyJWT`+`cryptography`, `httpx`) — não é pseudocódigo, só não foi exercitado contra o GitHub de verdade nesta sessão. |
| `git push` sob identidade da App | Testado contra um repo **bare local real** (`tests/test_pr_finalizer_idempotent.py::test_push_branch_uses_real_git_against_local_bare_remote`) — git de verdade, sem mock, só sem rede | Nada de lógica falta — só precisa do token real da installation (vem do `RealGitHubClient.authenticated_remote_url`, já implementado) |
| `SandboxHandle` → execução de comando | `LocalFakeSandbox` (subprocess local) em todos os testes | `SandboxHandle.container_id` populado pelo runtime real do WS-C; `DockerExecSandbox` já está implementado contra `docker exec` real, só não foi exercitado contra o runtime do WS-C (que está em paralelo) |
| Comandos de L1 (lint/typecheck/test/build) | Defaults genéricos Python (`ruff check .`, `mypy .`, `pytest -q`, `python -m compileall -q .`), configuráveis via env (`DSE_L1_*_CMD`) | Produção deveria derivar os comandos do PRÓPRIO repo alvo (Makefile/package.json/pyproject do repo que o Coder está editando) em vez de env vars fixas do processo do orchestrator — não implementado (não há ainda um contrato de "manifesto de projeto" publicado por nenhum workstream) |
| Secret-scan | Scanner próprio regex+entropia, real, sempre disponível (stdlib) | Poderia trocar por `detect-secrets` (já instalado no venv-wse como dependência de dev) para cobertura maior de padrões — troca é só na função `secret_scan_check`, mesma assinatura |
| WSE-E3-T8 "modo estrito" | **Implementado** (Fase 2) — push + compare link + adoção; testado contra Postgres real + `FakeGitHubClient` | Só falta exercitar contra `api.github.com` real (mesma pendência do PR normal — precisa da GitHub App registrada) |
| Sessão L2 (chamada de modelo, contexto fresco) | `FakeL2ReviewSession` (determinístico, sem LLM) — só a orquestração (recording/custo/guard/P3) é de produção | A sessão real é do WS-C (`dse_sandbox_runtime.l2.build_review_session`), em construção paralela; `build_l2_session()` já a resolve por import defensivo quando publicada |
| Interpretação de `review_state` em `source_ref` | Assume que WS-A anexa `review_state` (`approved`/`changes_requested`) a comentários de review formais do GitHub | Depende do formato real que `services/adapter-github` (WS-A) produzir — ajuste pontual quando integrado |

## Migração

`migrations/0006_wse.sql` (Fase 1, reservada para WS-E) cria: `validation_runs`
(evidência de cada execução L1), `wse_pr_tracking` (idempotência de PR por
`work_item_id`), `wse_comment_refs` (`CommentStateStore` do tracking comment),
`wse_ci_status` (último status de CI conhecido).

`migrations/0012_wse2.sql` (Fase 2, reservada para WS-E) adiciona:
`wse_l2_reviews` (evidência de cada turno L2: veredito, objeções, custo por
iteração), `wse_fix_loops` (contador durável do loop bounded L2->Coder), e
**altera** `wse_pr_tracking` de forma aditiva para o modo estrito (`pr_number`
passa a aceitar NULL + coluna `compare_url`). Aplicada com:

```
DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse python3 scripts/migrate.py
```

## Como rodar os testes

```bash
python3.12 -m venv /Users/saraiva/Documents/DSE/fase1/.venv-wse
source /Users/saraiva/Documents/DSE/fase1/.venv-wse/bin/activate
pip install -e /Users/saraiva/Documents/DSE/fase1/packages/contracts \
            -e /Users/saraiva/Documents/DSE/fase1/packages/dse_audit \
            -e /Users/saraiva/Documents/DSE/fase1/packages/dse_identity
pip install -e /Users/saraiva/Documents/DSE/fase1/services/validation
pip install pytest pytest-asyncio ruff mypy   # ruff/mypy só para os testes de L1 exercitarem os defaults reais
pytest -q /Users/saraiva/Documents/DSE/fase1/services/validation
```

Requer a infra da fundação no ar (Postgres em `localhost:5432` com
`migrations/0001_foundation.sql` E `0006_wse.sql` aplicadas, Temporal em
`localhost:7233`) — os testes de idempotência de PR e de audit usam Postgres
real, e os testes de `review_signal` usam Temporal real (nunca mockados,
por design: são as garantias de durabilidade/idempotência do próprio sistema).

## Resultado real da última execução

```
71 passed in ~12s   (45 Fase 1 + 26 Fase 2)
```

Fase 2 adicionou `tests/test_l2_review.py` (6), `tests/test_fix_loop.py` (11),
`tests/test_strict_mode.py` (9) — todos contra Postgres real (audit rows de
`l2_review_run`/`l2_fix_retry`/`l2_fix_loop_exhausted`/`pr_compare_link_posted`/
`pr_adopted` conferidos no `audit_log` real, P8). Nenhum teste mockado para
Postgres/Temporal. Nenhum teste skipado. Tabelas usam `tenant_id`/`work_item_id`
únicos via `uuid4()` por teste.

## Pendências conhecidas (declaradas, não escondidas)

1. **Sessão L2 real (WS-C)** — a chamada de modelo de contexto fresco é dona do
   WS-C (`ACTIVITY_RUN_L2_REVIEW` / `dse_sandbox_runtime.l2`), em construção
   paralela. `build_l2_session()` já a resolve por import defensivo; os testes
   usam `FakeL2ReviewSession` (marcado como fixture). Encaixe físico (import
   cruzado, registro no Worker único) precisa de uma passada de integração
   quando os merges convergirem.
2. **Integração real com WS-C** (`DockerExecSandbox`) e **WS-B** (nomes exatos
   dos campos dos modelos de input das Activities, e quem é dono do contador do
   fix-loop no replay) não foi testada end-to-end pelo mesmo motivo — a
   interface está pronta e documentada. Nota de design: o **estado** do loop de
   fix-retries é do workflow do WS-B (durável via event history); `wse_fix_loops`
   é um espelho de evidência/observabilidade. `wse_record_fix_loop` deriva o
   estado-antes-da-iteração de `iterations`, então um replay que reexecute a
   Activity idempotentemente converge — mas o WS-B deve tratar o contador como
   seu (não somar em cima do que a Activity retorna).
3. **GitHub App real** não testada contra `api.github.com` de verdade (sem
   App registrada nesta sessão) — `RealGitHubClient` está implementado contra
   a API real, mas só o `FakeGitHubClient` foi exercitado nos testes (inclui o
   modo estrito e a adoção de PR).
4. **Comandos de L1 fixos via env** em vez de derivados do repo alvo — ver
   tabela acima.
5. **Flag de modo estrito por env** em vez de `tenant_config` (WS-F) — quando
   a tabela de flags por tenant existir, trocar só `StrictModeConfig.is_strict_for`.
