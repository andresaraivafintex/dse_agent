# services/sandbox-runtime (WS-C)

Sandbox efêmero por tarefa (Docker rootless), lifecycle como Temporal
Activities, interface de substrato de agente + adapter OpenHands, e sessão
Coder com git de escopo limitado. Ver `CONVENTIONS.md` na raiz do monorepo
para o contrato entre workstreams.

## O que está implementado e funcionando (testado contra Docker/Postgres reais)

### WSC-E1 — Sandbox efêmero por tarefa

- **T1 — Isolamento** (`docker_driver.py`): todo container de sandbox roda
  `--user <uid não-root>`, `--read-only` + `--tmpfs /tmp`, `--cap-drop ALL`,
  `--security-opt no-new-privileges`, sem bind de `/var/run/docker.sock`, e
  conectado exclusivamente à rede Docker interna `dse_sandbox_net`
  (`internal=True` — sem gateway de internet). O único host alcançável de
  dentro do sandbox é o egress-proxy (`services/egress-proxy`), que por sua
  vez está conectado tanto a `dse_sandbox_net` quanto a `dse_net` (que tem
  rota de internet padrão do Docker).
  Provado por `tests/test_network_isolation.py`, que sobe containers Docker
  de verdade (sandbox + egress-proxy + um "upstream" simulando a internet) e
  prova: (a) nenhum mount referencia `docker.sock`; (b) `id -u` dentro do
  sandbox não é `0`; (c) uma requisição direta a um host externo falha; (d)
  uma requisição via egress-proxy a um host permitido funciona; (e) uma
  requisição via egress-proxy a um host fora da allowlist volta `403`.
- **T2 — Caps de recurso + métricas OTel** (`docker_driver.ResourceCaps`,
  `metrics.py`): `--cpus`/`--memory`/`--pids-limit` derivados do `budget` do
  `WorkItem` (chaves `cpu_limit`/`memory_mb`/`pids_limit`/`resource_class`,
  com defaults por `resource_class` em `small`/`medium`/`large`).
  `teardown_sandbox` emite um histograma OTel
  `dse.sandbox.runtime_minutes` com os atributos
  `dse_contracts.constants.OTEL_ATTR_TENANT/WORK_ITEM/STAGE` +
  `dse.resource_class`. Provado por
  `tests/test_resource_caps_and_metrics.py` (inspeciona `HostConfig` real do
  container e lê os data points via `InMemoryMetricReader` do OTel SDK).
- **T3 — Lifecycle como Temporal Activities** (`activities.py`):
  `provision_sandbox`/`checkpoint_sandbox`/`rebuild_sandbox`/
  `teardown_sandbox` decoradas `@activity.defn(name=ACTIVITY_*)` com os
  nomes exatos de `dse_contracts.activities`, retornando
  `SandboxHandle`/`CheckpointRef`. Idempotência: `provision_sandbox`
  procura por label `dse.work_item_id` antes de criar — chamado duas vezes
  para o mesmo `work_item_id`, reaproveita o mesmo container (provado por
  `tests/test_idempotent_provision.py`). `tests/test_temporal_activity_wiring.py`
  prova que são Activities de verdade do Temporal SDK (nomes batem com o
  contrato, executam via `temporalio.testing.ActivityEnvironment` — o
  harness oficial do SDK para testar uma Activity isolada, não um mock).
- **T4 — Checkpoint/rebuild + chaos** (`git_checkpoint.py`,
  `scoped_git.py`): checkpoint = commit + push do branch da tarefa para um
  bare repo git local (`git init --bare`, servindo de "origin" de teste —
  não é um remoto real, conforme permitido pelo enunciado). Rebuild clona o
  bare repo e faz `git checkout` do sha do checkpoint num workspace novo.
  `tests/test_checkpoint_chaos.py` mata o container no meio (`docker kill`)
  e prova que o rebuild recupera os commits sem perda.

### WSC-E3 — Substrato de agente + sessão Coder

- **T1 — Interface + adapters** (`substrate.py`): `AgentSubstrate` é um
  `Protocol` com `create_session`/`run_turn`/`collect_artifacts`.
  `FakeSubstrate` é um adapter in-memory determinístico (roteirizado por
  turno) usado por todos os testes — não faz nenhuma chamada de rede/modelo.
  `OpenHandsSubstrate` é o adapter real sobre o pacote PyPI `openhands-sdk`
  (`pip install openhands-sdk` **funcionou nesta sessão**, v1.21.0 — ver
  "Limitações conhecidas" abaixo para o que falta pra exercitar um turno de
  verdade). O LLM do OpenHands é sempre construído com
  `base_url=<model-gateway>` + `api_key=<virtual key>` +
  `extra_headers=GatewayCallHeaders(...).to_http_headers()` — nunca aponta
  para um SDK/endpoint de provider diretamente.
- **T2 — `run_coder_turn` com git de escopo limitado**
  (`activities.py::run_coder_turn`, `scoped_git.py`): o substrato SÓ edita
  arquivos no workspace — o commit/push para o branch da tarefa é feito
  depois, por código determinístico (`ScopedGitSession`), nunca pelo LLM
  (P1). Duas camadas de enforcement contra force-push/PR/branch errado:
  1. **Toolset**: `ScopedGitSession` expõe só `.commit()`/`.push()` (refspec
     hardcoded), sem `run_git_command`/`create_pull_request`/force-push.
  2. **Escopo do remoto**: um hook `pre-receive` REAL (`install_pre_receive_guard`)
     instalado no bare repo de checkpoint recusa qualquer ref fora do branch
     da tarefa e qualquer non-fast-forward — mesmo que alguém contorne
     `ScopedGitSession` e rode `git push --force` cru.
  3. **Escopo da credencial**: `egress_proxy.credentials.ScopedCredential`
     nunca tem `pull_requests:write`/força — `create_pull_request()`/
     `force_push()` sempre levantam `GitHubScopeError`.
  Provado adversarialmente por `tests/test_run_coder_turn_scoped_git.py`
  (força push cru → rejeitado pelo hook; push pra outro branch → rejeitado;
  `ScopedGitSession.push()` propaga a recusa como `GitScopeViolation`).

## Fase 2 ("Judgment & queue") — o que a Fase 2 adicionou (WSC-E3-T3/T4/T5, E4, E5)

Extensão natural da fundação da Fase 1 (`AgentSubstrate` + `ScopedGitSession` +
Activities Temporal). Migração própria: `migrations/0010_wsc2.sql`
(`skill_registry`, `retrieval_documents`). Novos módulos:
`skill_registry.py`, `retrieval.py`, `toolsets.py`, `sessions.py`, e 3 novas
Activities em `activities.py` (na lista `ACTIVITIES` que o worker do WS-B
importa).

### WSC-E4-T1 — Skill registry bootstrap (`skill_registry.py`)

Tabela `skill_registry` tenant-scoped, semeada com skills curadas por humano
(`created_by` = principal humano, nunca `system:*`). `read_approved_skills(tenant_id,
task_class=…)` é a API lida pelo Planner: só devolve `status='approved'` do
tenant pedido — rascunhos (`draft`) e skills de outro tenant NUNCA vazam
(isolamento hardcoded na query; provado por `tests/test_skill_registry.py`,
inclusive com dois tenants dinâmicos de mesma `skill_key`). SEM pipeline de
promoção (isso é Fase 4) — só o registry + a leitura, como o escopo manda.

### WSC-E5 — Retrieval/index service (`retrieval.py`, ADR-24)

`RetrievalService` sobre `retrieval_documents` (tenant-scoped) com três
capacidades sobre o mesmo índice: **repo map** (arquivos + símbolos top-level
extraídos por regex leve multi-linguagem), **busca lexical** (BM25) e
**embeddings self-hosted** (TF-IDF esparso + cosseno — sem GPU nesta sessão;
ver "O que falta para produção"). `index_repo` é idempotente (upsert por
`content_sha`). **ISOLAMENTO POR TENANT RIGOROSO**: `_require_tenant` recusa
tenant vazio; toda query filtra `tenant_id = %s`; não há caminho de leitura
cross-tenant nem "list all" (provado por
`tests/test_retrieval.py::test_tenant_isolation_strict` — índice de um tenant é
invisível a outro; coordenado com a suíte de isolamento do WS-F).
**Conteúdo indexado é input NÃO CONFIÁVEL do Planner**: `RetrievalHit.trusted`
é sempre `False` e `render_untrusted_context` embrulha os trechos num bloco
claramente demarcado com instrução de tratar como DADO, nunca como comando
(defesa contra prompt-injection vinda de código/ticket indexado; o Planner é
read-only, então nem um payload malicioso consegue disparar escrita).

### WSC-E3-T3 — Sessão Planner read-only (`run_planner_turn`, `sessions.py`, `toolsets.py`)

Activity `run_planner_turn` (nome `ACTIVITY_RUN_PLANNER_TURN`): toolset SÓ
leitura (`PlannerToolset`). Hidrata AGENTS.md + CODEOWNERS (do workspace),
skill registry aprovado do tenant (E4), tickets relacionados e o
retrieval/index (E5), e emite um `PlanArtifact` estruturado (steps,
expected_files, diff_budget_lines, test_plan, risk_class). **P1**: o
`risk_class` — que dirige o gate do WS-B — é DERIVADO por
`classify_risk_class` (código determinístico sobre o blast radius declarado:
forbidden_paths, globs de alto risco como `**/*auth*`/`**/migrations/*`,
tamanho do diff), NÃO pela palavra do LLM; o proposer (LLM) só sugere
steps/expected_files/test_plan. **Conformidade** (provada por
`tests/test_planner_session.py`): qualquer tool de ESCRITA no Planner FALHA
com `ToolPermissionError` (a sessão passa toda tool-call por
`Toolset.check` antes de despachar — o teste roda um `exploration_script` com
um `write_file` e prova que levanta e que o arquivo nunca é criado).

### WSC-E3-T4 — Sessão Tester (`run_tester_turn`)

Activity `run_tester_turn` (nome `ACTIVITY_RUN_TESTER_TURN`): `TesterToolset`
permite leitura + `run_tests` + `write_file` SÓ em caminhos de teste (`tests/`,
`test_*.py`, `*_test.py`, `conftest.py`, `*.test.ts`, `_test.go`); escrever em
código de produção FALHA (`ToolPermissionError`). Os testes escritos EXECUTAM
de verdade — `run_tests` roda `pytest` real dentro do workspace e reporta
pass/fail (um teste que falha é reportado como falha, provando que a execução é
real, não simulada). O commit/push dos test files é determinístico
(`ScopedGitSession`, identidade `dse-tester`), nunca pelo LLM (P1). Provado por
`tests/test_tester_session.py`. Retorno: `TesterTurnResult` (não está em
`packages/contracts` porque WS-C não edita a fundação — ver "Gaps" abaixo).

### WSC-E3-T5 — Sessão Reviewer fresh-context (`run_l2_review`)

Activity `run_l2_review` (nome `ACTIVITY_RUN_L2_REVIEW`, retorna `L2Verdict` de
`dse_contracts`): sessão NOVA (`FreshReviewerSession`) que recebe SÓ o
`ReviewerContext(plan, diff)` — **NADA do histórico do Coder (P3)**. A prova é
POR CONSTRUÇÃO (`tests/test_reviewer_fresh_context.py`): os campos do
`ReviewerContext` e da entrada `RunL2ReviewInput` são exatamente `{plan, diff}`
(+ ids/classes) — não existe campo/parâmetro que carregue transcrição, turnos,
thoughts ou tool-calls do produtor; a sessão fresca só expõe
`read_plan`/`read_diff` (sem `repo_map`/`search_code`/history). Retorna
`L2Verdict` (passed + objeções específicas arquivo/linha). O veredito L2 é uma
RECOMENDAÇÃO que gateia a progressão (o WS-E orquestra o loop de fix-retries em
torno dela) — o merge continua humano (P1), e por ser sessão fresca nunca é o
produtor aprovando o próprio trabalho (P3).

### Testes da Fase 2 (reais, contra Postgres/Docker/Temporal SDK)

`test_skill_registry.py` (5), `test_retrieval.py` (7),
`test_planner_session.py` (5), `test_tester_session.py` (4),
`test_reviewer_fresh_context.py` (6) — **27 testes novos**, todos com infra
real (Postgres para registry/índice; Docker para o workspace do sandbox das
sessões Planner/Tester; `temporalio.testing.ActivityEnvironment` para provar
que as 3 novas Activities são Activities Temporal de verdade com os nomes do
contrato). **Resultado real desta sessão: `42 passed` em sandbox-runtime**
(15 da Fase 1 + 27 da Fase 2) + `13 passed` em egress-proxy = **55 passed, 0
failed, 0 skipped**.

### Gaps da Fase 2 (documentados, não escondidos)

- **Substrato real das sessões Planner/Tester**: como na Fase 1 (Coder), o
  substrato roteirizado (`ScriptedAgentSession`) é o usado nos testes — não
  chama LLM. Em produção o adapter OpenHands registra só as ferramentas cujo
  nome está no allowlist do toolset e roteia cada tool-call pelo mesmo
  `Toolset.check`, e o proposer do Planner / o `verdict_fn` do Reviewer viram
  saídas de uma `Conversation` OpenHands fresca. Wireável por
  `_run_planner_turn_impl(..., proposer=…)`, `_run_tester_turn_impl(...,
  authoring_script=…)`, `_run_l2_review_impl(..., verdict_fn=…)` — mesmos
  pontos de injeção do `_run_coder_turn_impl` da Fase 1.
- **Embeddings**: TF-IDF esparso (self-hosted, sem GPU) é o "modelo local
  pequeno" permitido pelo enunciado. Troca por um encoder denso (ex.:
  `sentence-transformers/all-MiniLM-L6-v2` em CPU) é ADITIVA — mesma interface
  `EmbeddingModel`, mesma coluna `embedding` (JSONB). Documentado no topo de
  `retrieval.py`.
- **`TesterTurnResult` fora do contrato**: WS-C não pode editar
  `packages/contracts`. `run_tester_turn` retorna um `pydantic.BaseModel` local
  (`activities.TesterTurnResult`); WS-B consome via dict/`model_validate`.
  Proposta de promoção ao contrato (aditiva) fica para a próxima janela do
  arquiteto (regra do CONTRACTS-CHANGELOG). `PlanArtifact` e `L2Verdict` já
  estavam no contrato.
- **`egress-proxy` (WS-C)**: sem mudanças na Fase 2 — o escopo WS-C da Fase 2
  (E3-T3/T4/T5, E4, E5) é todo em `sandbox-runtime`. O proxy default-deny +
  injeção de credencial efêmera da Fase 1 continua válido e é a rota de rede
  das novas sessões (LLM sempre via model-gateway).

## Fase 3 ("Evidence") — o que a Fase 3 adicionou (WSC-E3-T4b, WSC-E3-T6)

Sem migração nova (`0015_wsc3.sql` reservado, NÃO criado — nenhuma tabela
nova foi necessária) e sem mudança no egress-proxy (a toolchain Playwright é
instalada em BUILD TIME da imagem; em runtime o sandbox continua sem
internet — nenhuma entrada nova de allowlist).

### WSC-E3-T4b — Playwright no sandbox + convenção `demos/<work_item_id>/` (P0)

- **(a) Imagem base com toolchain Playwright real**
  (`docker/Dockerfile.sandbox-base`): base pinada em
  `python:3.11-slim-bookworm` (a tag `-slim` migrou para trixie, que o
  `playwright install --with-deps` não suporta — falha real observada e
  documentada no Dockerfile), node/npm do apt bookworm,
  `@playwright/test@1.49.1` + `playwright@1.49.1` pinados (P7) em
  `/opt/dse-playwright`, chromium headless via
  `npx playwright install --with-deps chromium` em
  `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers` (world-readable — o uid de
  runtime é aleatório não-root), `HOME=/tmp`, e symlink `/node_modules` →
  toolchain (o `npx` do npm 9 resolve por local prefix, não pelo `$PATH`;
  sem o symlink ele tentaria INSTALAR do registry — impossível sem internet
  no sandbox; falha real observada e testada). **Imagem buildada de verdade
  nesta sessão: `dse-sandbox-base:wsc3`, 2.35GB** (custo do chromium +
  deps de sistema; a imagem da Fase 1 `python:3.11-slim`+git continua sendo
  o default para sessões que não produzem evidência — a imagem é parâmetro
  de `provision_sandbox`).
- **(b) Convenção `demos/<work_item_id>/` no TesterToolset**
  (`toolsets.py::demo_dir_for/is_demo_path`): path de escrita PERMITIDO
  adicional, ESCOPADO ao work item da sessão. Endurecimento descoberto por
  teste: dentro de `demos/` a regra genérica de test path (`*.spec.js` em
  qualquer lugar, herdada da Fase 2) NÃO se aplica — senão o Tester de uma
  tarefa escreveria no demo de outra só nomeando o arquivo `*.spec.js`.
  Bloqueados e provados: código de produção, `demos/` de OUTRO work item,
  path traversal (`demos/<wi>/../../src/...`), e qualquer write em `demos/`
  numa sessão sem work item (`tests/test_tester_demo_convention.py`).
- **(c) Fixture `@demo` determinístico** (`demo_fixture.py`): template
  commitado (page HTML estática com interação visível + spec Playwright com
  tag `@demo` + `playwright.config.js` com `video: 'on'`/`trace: 'on'` e
  `webServer: python3 -m http.server` — página SERVIDA localmente dentro do
  container, não `file://`). O Tester roteirizado o autora via
  `demo_authoring_script(work_item_id)` — autor fixture, artefatos reais.
  `DSE_DEMO_BASE_URL` liga o mesmo spec a um preview real
  (`TriggerPreview`/`PreviewRef.url` do WS-B/WS-E) quando houver.
- **Aceitação REAL** (`tests/test_demo_playwright_in_sandbox.py`):
  `npx playwright test --grep @demo` executado DENTRO de um container
  provisionado pelo `docker_driver` de produção (rootless uid 10001,
  `--read-only`, `--cap-drop ALL`, rede interna SEM internet,
  `budget={"resource_class": "large", "tmp_mb": 512}`) termina `1 passed` e
  grava **vídeo `.webm` real** (+ `trace.zip`) no workspace. Nota de
  formato: Playwright grava `.webm` nativamente (não mp4); transcodificação
  é pós-processamento do pipeline do WS-E se a superfície exigir —
  documentado em `demo_fixture.py`, combinado com o path da convenção que o
  WS-E consome (`RunDemoEvidenceInput.demo_dir` default
  `demos/<work_item_id>/`).
- Mudança aditiva no driver: `ResourceCaps.tmp_mb` (budget key `tmp_mb`,
  default 64MB inalterado) — chromium precisa de mais scratch em `/tmp`.

### WSC-E3-T6 — Segundo substrato: Claude Agent SDK + conformidade (P1)

- **`ClaudeAgentSubstrate`** (`substrate.py`): adapter real sobre o pacote
  PyPI `claude-agent-sdk` (**`pip install claude-agent-sdk` funcionou nesta
  sessão — v0.2.124**; o wheel embute o CLI). Gateway-only pelo mesmo
  triângulo do OpenHands: `ANTHROPIC_BASE_URL=<model-gateway>`,
  `ANTHROPIC_API_KEY=<virtual key por tarefa>`, `ANTHROPIC_CUSTOM_HEADERS=`
  headers do contrato (`GatewayCallHeaders`) — nunca endpoint de provider.
  Toolset restrito a `Read/Write/Edit/Glob/Grep` (sem Bash/git/PR — P1: o
  commit/push continua determinístico na Activity), `setting_sources=[]`
  (sessão hermética, nada do host).
- **Troca de substrato é CONFIG POR DEPLOYMENT**
  (`substrate.substrate_from_env`, env `DSE_CODER_SUBSTRATE` em
  `fake|openhands|claude-agent`, default `fake`): a Activity
  `run_coder_turn` constrói via a factory; o workflow do WS-B chama a
  Activity por nome e não conhece o substrato — zero mudança de código de
  workflow para trocar. Nome desconhecido = `ValueError` limpo (P6), nunca
  fallback silencioso.
- **Suíte de conformidade** (`tests/test_substrate_conformance.py`): os
  mesmos testes parametrizados contra `OpenHandsSubstrate` E
  `ClaudeAgentSubstrate` (ambos os SDKs REAIS instalados neste venv):
  protocolo `AgentSubstrate`, base_url == gateway e nenhum fragmento de
  endpoint de provider em nenhum lugar da config, headers de policy/budget
  do contrato presentes (caps no call time do WS-D), superfície sem
  git/PR/bash, erro limpo sem sessão, e seleção por env (incluindo o ponto
  de construção da Activity). É a compatibility suite do NFR-09 — pins de
  piso em `pyproject.toml [project.optional-dependencies].substrates`.
- **Limite honesto (igual ao OpenHands desde a Fase 1)**: nenhum turno com
  inferência REAL é exercitado — exige o model-gateway atendendo uma
  virtual key válida com provider de verdade. A conformidade cobre
  construção/wiring/seleção; o turno real fica para a janela de integração
  com o WS-D.

### Testes da Fase 3 (reais, contra Docker/Postgres + SDKs instalados)

`test_tester_demo_convention.py` (6), `test_demo_playwright_in_sandbox.py`
(1 — o aceite ponta-a-ponta com vídeo real), `test_substrate_conformance.py`
(16, parametrizados) — **23 testes novos**. **Resultado real desta sessão:
`65 passed` em sandbox-runtime** (15 Fase 1 + 27 Fase 2 + 23 Fase 3) +
`13 passed` em egress-proxy + `14 passed` em packages/contracts (boundary —
inalterados, nenhum call site de workflow mudou) = **0 failed, 0 skipped**.

## Fase 4 ("Loop hardening & learning") — pipeline de promoção de skill (WSC-E4-T2/T3)

Fecha o que a Fase 2 deixou de fora de propósito: a curadoria automática de
skills a partir de execuções. Tudo DETERMINÍSTICO (P1) — nenhuma decisão de
fluxo por LLM. Migrações: `0019_wsc4.sql` (gate de entrada, já aplicado —
status ampliado, `version`, `skill_episode`, `skill_eval`) + `0020_wsc4.sql`
(multi-versão + proveniência, ver abaixo).

### WSC-E4-T2 — Captura de episódios + materialização de candidates (`skill_promotion.py`)

`record_episode(tenant, source, pattern_key, ...)` grava as três *sources at
launch* (§10.17) em `skill_episode`: `clarification` (WS-B), `ci_repair` e
`review_feedback` (WS-E). Quando um `pattern_key` acumula
`SUM(occurrence_n) >= threshold` (`DSE_SKILL_CANDIDATE_THRESHOLD`, default 3 —
CONFIG, não LLM), `materialize_candidates(tenant)` cria uma skill
`status='candidate'`, `version` incrementada, `pattern_key` + `provenance`
completos (de quais episódios/work-items/fontes veio). Idempotente (não
re-materializa uma skill que já tem versão viva). Um candidate NÃO é servido ao
Planner e NÃO se auto-promove — precisa de eval + aprovação humana (P3).
`created_by='system:skill-promotion'` de propósito: um candidate é PROPOSTA da
máquina; só a aprovação humana o torna servível.

### WSC-E4-T3 — Esteira de promoção governada (`eval_skill_candidate`, `promote_skill`)

Duas Activities Temporal (nomes/tipos de `dse_contracts.activities`, gate de
entrada da Fase 4), lógica em `skill_promotion.py`:

- **`eval_skill_candidate`** → `EvalSkillCandidateResult`: replay do candidate
  contra um eval set histórico (positivos = casos onde a skill ajudaria;
  negativos = casos onde não deve disparar; derivado de `skill_episode` ou
  injetado). `negative_regressions > 0` ⇒ `passed=False`. Grava a trilha em
  `skill_eval` (P8).
- **`promote_skill`** → `PromoteSkillResult`: máquina de estados explícita
  `candidate → approved → canary → active` (+ rollback `active/canary →
  rolled_back`). Invariantes **por construção** (levantam exceção ANTES de
  qualquer escrita — não há code path que as viole):
  - **P1/P3**: `to_status in {approved, active}` sem `approver` humano resolvido
    (vazio ou `system:*`) ⇒ `ApproverRequired`. Promoção sem humano nomeado é
    IMPOSSÍVEL — o teste adversarial prova (`promote_skill(to_status=active,
    approver=None)` recusa).
  - `candidate → approved` sem eval passante (`negative_regressions=0`) ⇒
    `EvalGateNotPassed` — o candidate não se aprova sozinho.
  - transição fora da máquina ⇒ `IllegalTransition`.
  - Toda transição → `dse_audit.emit` com a identidade do aprovador.
- **Rollback = mudança de PONTEIRO em uma transação** (failure mode 13): a
  versão servida vira `rolled_back` e a versão que ela suplantou (registrada em
  `provenance.supersedes`) volta a `active` — em segundos, sem reprocessar.

### O que o Planner enxerga (`read_approved_skills`)

O Planner de produção lê `status IN ('approved','active')`. `candidate`,
`canary`, `draft`, `rolled_back`, `retired` NUNCA são servidos. **`canary` =
shadow nesta fase** — não há seleção de subconjunto canário; um canary é
avaliado fora da linha de produção e só passa a servir ao virar `active`. O
índice único parcial `uq_skill_registry_one_served` (migração 0020) garante
ESTRUTURALMENTE (não por convenção) no máximo UMA versão servida por
`(tenant, skill_key)`: a transição que serve uma versão nova rebaixa a anterior
na mesma transação, ou o índice rejeita — o Planner nunca vê duas versões da
mesma skill.

### Migração `0020_wsc4.sql`

Aditiva. A 0010 tinha `UNIQUE (tenant_id, skill_key)` (UMA linha por skill); o
rollback "por ponteiro sem perder a versão anterior" (comentário da 0019) exige
que várias versões coexistam como linhas distintas. A 0020 troca a unicidade
para `(tenant_id, skill_key, version)`, adiciona `pattern_key`/`provenance` e o
índice único parcial de "uma versão servida".

### Testes da Fase 4 (reais, contra Postgres)

- `test_skill_promotion.py` (7): as 3 sources; limiar (noop abaixo, materializa
  no limiar); candidate nasce não-servido com proveniência; idempotência; eval
  passa sem regressão e reprova com regressão negativa (grava `skill_eval`).
- `test_promotion_pipeline.py` (7) — **exit da Fase 4**: fluxo completo
  candidate→eval→approved→canary→active→rollback com o ponteiro voltando à
  versão anterior; rollback sem versão anterior some do Planner; adversariais
  P1/P3 (approver `None`/`""`/`system:*` recusam); gate de eval; transição
  ilegal.
- `test_promotion_activities_wiring.py` (3): as duas Activities via
  `temporalio.testing.ActivityEnvironment` (SDK real) + a recusa propagada.

**17 testes novos**; suíte do pacote **82 passed, 0 failed** nesta sessão.

## O que está com fixture/mock local (documentado, não escondido)

- **`FakeSubstrate`** é o substrato usado em TODOS os testes desta suíte —
  não faz chamada de modelo nenhuma, edita arquivos a partir de um script
  Python fornecido pelo teste. Isso é deliberado: testar a plumbing real
  (Docker + git + Temporal) sem depender do model-gateway do WS-D estar de
  pé nem gastar inferência real.
- **`model_gateway_client.mint_virtual_key`**: tenta `POST
  {DSE_MODEL_GATEWAY_URL}/internal/virtual-keys` (default
  `http://localhost:4000`, a porta reservada do WS-D). Se o endpoint não
  responder (WS-D ainda não estava de pé quando este workstream rodou seus
  testes), cai para uma virtual key de fixture (`fixture-vk-<work_item_id>-
  <random>`) — isso é **claramente sinalizado** no campo `fixture: bool` do
  retorno e usado nos testes. Desabilite com
  `DSE_MODEL_GATEWAY_ALLOW_FIXTURE=0` para forçar falha limpa (P6) em vez de
  fixture silenciosa.
- **Container Temporal da infra compartilhada está fora do ar nesta sessão**:
  `docker ps -a` mostra `dse_temporal` como `Exited (1)` — a imagem
  `temporalio/auto-setup:1.24` reclama de
  `config/dynamicconfig/development-sql.yaml` ausente. Isso é
  `docker-compose.yml` (fundação, fora do escopo de edição do WS-C) — não
  tentei consertar. Por isso os testes de Activity usam
  `temporalio.testing.ActivityEnvironment` (o harness oficial do SDK, real,
  não mock) em vez de um Worker conectado a um servidor Temporal de
  verdade — o SDK e a lógica das Activities são 100% reais, só não há
  workflow/worker rodando ponta a ponta nesta sessão.

## O que falta para produção

- **`OpenHandsSubstrate` com execução de ferramentas realmente dentro do
  sandbox**: hoje usa `openhands.sdk.LocalWorkspace` (executa no processo
  que roda o SDK, isto é, no worker do WS-B). Produção deveria trocar por
  `openhands.sdk.RemoteWorkspace`, apontando para um `openhands-agent-server`
  (também disponível como dependência transitiva do `openhands-sdk`, pacote
  `openhands-agent-server`) rodando DENTRO do container provisionado por
  `docker_driver.py` — só assim a execução de bash/edição de arquivo do
  agente fica de fato dentro do sandbox isolado. Não implementado por não
  ser exercitável sem o model-gateway do WS-D de pé com um provider real
  configurado no LiteLLM.
- **Imagem de sandbox** (`docker/Dockerfile.sandbox-base`): desde a Fase 3
  ela é BUILDADA e EXERCITADA de verdade (`dse-sandbox-base:wsc3`, 2.35GB —
  o teste de aceitação do `@demo` roda `npx playwright` via `docker exec`
  dentro dela), mas continua não publicada em registry (build local; os
  testes buildam se a tag não existir). Os cenários de checkpoint/scoped-git
  seguem rodando os comandos git contra o path do host que é o mesmo bind
  mount do workspace do container (documentado em `git_checkpoint.py`);
  produção deveria publicar a imagem num registry e rodar git via
  `docker exec` também.
- **Integração real com `services/model-gateway` (WS-D)**: `mint_virtual_key`
  já tenta o endpoint real primeiro; a integração ponta-a-ponta acontece na
  fase de integração entre workstreams, quando o WS-D estiver publicando
  `/internal/virtual-keys` de verdade.
- **`sandbox_leases`/`egress_credential_leases` (migração `0004_wsc.sql`)**
  são bookkeeping operacional adicional (além do `audit_log` via
  `dse_audit.emit`, que é o registro obrigatório de P8) — best-effort, cai
  silenciosamente para "sem persistência extra" se o Postgres não estiver
  alcançável (nunca quebra o path principal).

## Como rodar os testes

```bash
python3.12 -m venv .venv-wsc
source .venv-wsc/bin/activate
pip install -e packages/contracts -e packages/dse_audit -e packages/dse_identity
pip install -e services/sandbox-runtime -e services/egress-proxy
pip install pytest

DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse python3 scripts/migrate.py

DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse \
  pytest -q services/sandbox-runtime/tests
```

Requer Docker rodando (containers reais são criados/destruídos) e Postgres
da fundação em `localhost:5432` (não precisa do Temporal server — ver nota
acima sobre `ActivityEnvironment`).

**Resultado real na sessão da Fase 3**: `65 passed` neste pacote (15 Fase 1 +
27 Fase 2 + 23 Fase 3) / `78 passed` somando `services/egress-proxy` (13), `0
failed`, `0 skipped` (os testes condicionais de `OpenHandsSubstrate` e
`ClaudeAgentSubstrate` rodam de verdade porque `openhands-sdk` e
`claude-agent-sdk` instalaram com sucesso neste ambiente). O teste de
aceitação do `@demo` exige a imagem `dse-sandbox-base:wsc3` (builda sozinho
na primeira vez — minutos; cacheada depois).
