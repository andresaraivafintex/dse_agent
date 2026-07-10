# services/validation — WS-E (Validação L1 + PR finalizer)

Fintex DSE, Fase 1 ("Core loop"). Implementa o `services/validation/` descrito em
`CONVENTIONS.md`: pipeline L1 (lint/typecheck/test/build + SAST/secret-scan +
diff-budget/forbidden-paths), PR finalizer idempotente, consumo mínimo de
status de CI, e o handler de resume-por-review-comment (UC4).

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
- **T8 (P1, opcional) — "modo estrito"**: **NÃO implementado**. `config.StrictModeConfig`
  já expõe a flag de env (`DSE_WSE_STRICT_MODE`), mas ela não está conectada
  a `finalize_pr_core` porque o contrato publicado (`dse_contracts.activities.PrRef`)
  exige `pr_number: int` obrigatório — não há como a Activity
  `ACTIVITY_FINALIZE_PR` retornar "só um compare link" sem um PR sem alterar
  esse tipo (que não podemos editar sem coordenar com o arquiteto). Documentado
  como pendência real, não escondido: para implementar de verdade,
  `PlanArtifact`/`PrRef` precisariam de um campo opcional (`compare_url: str | None`)
  ou uma Activity separada `ACTIVITY_PROPOSE_COMPARE_LINK`.

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
| WSE-E3-T8 "modo estrito" | **Não implementado** — só a flag de config existe | Requer campo novo em `PrRef`/`PlanArtifact` (ver acima) — decisão de contrato fora do escopo de WS-E sozinho |
| Interpretação de `review_state` em `source_ref` | Assume que WS-A anexa `review_state` (`approved`/`changes_requested`) a comentários de review formais do GitHub | Depende do formato real que `services/adapter-github` (WS-A) produzir — ajuste pontual quando integrado |

## Migração

`migrations/0006_wse.sql` (reservada para WS-E) cria: `validation_runs`
(evidência de cada execução L1), `wse_pr_tracking` (idempotência de PR por
`work_item_id`), `wse_comment_refs` (`CommentStateStore` do tracking comment),
`wse_ci_status` (último status de CI conhecido). Aplicada com:

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
45 passed in ~9s
```

Nenhum teste mockado para Postgres/Temporal. Nenhum teste skipado. Reexecutei
a suíte duas vezes seguidas para confirmar que não há vazamento de estado
entre execuções (tabelas usam `tenant_id`/`work_item_id` únicos via `uuid4()`
por teste, exceto onde o próprio ponto do teste é reconciliar estado
persistente — nesses casos, o teste usa uuid único por chamada também).

## Pendências conhecidas (declaradas, não escondidas)

1. **T8 (modo estrito)** — não implementado, ver tabela acima.
2. **Integração real com WS-C** (`DockerExecSandbox`) e **WS-B** (nomes exatos
   dos campos dos modelos de input das Activities) não foi testada end-to-end
   porque os dois workstreams estão em desenvolvimento paralelo nesta mesma
   sessão — a interface está pronta e documentada, mas o "encaixe" físico
   (import cruzado, registro no Worker único) precisa de uma passada de
   integração depois que os três merges convergirem.
3. **GitHub App real** não testada contra `api.github.com` de verdade (sem
   App registrada nesta sessão) — `RealGitHubClient` está implementado contra
   a API real, mas só o `FakeGitHubClient` foi exercitado nos testes.
4. **Comandos de L1 fixos via env** em vez de derivados do repo alvo — ver
   tabela acima.
