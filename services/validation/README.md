# services/validation — WS-E (Validação L1/L2/L3 + PR finalizer + evidência)

Fintex DSE. Implementa o `services/validation/` descrito em `CONVENTIONS.md`:
pipeline L1 (lint/typecheck/test/build + SAST/secret-scan + diff-budget/
forbidden-paths), PR finalizer idempotente, consumo mínimo de status de CI, e
o handler de resume-por-review-comment (UC4). **Fase 2 ("Judgment & queue")**
adiciona: orquestração do loop L2 de contexto fresco (WSE-E2-T4), loop de
fix-retries bounded L2->Coder (WSE-E2-T5), e o **modo estrito de PR** em que um
humano abre o PR (WSE-E3-T8, desbloqueado pelo `PrRef.compare_url` novo).

> **Fase 4 — resumo do que foi adicionado** (detalhe abaixo em §Fase 4):
> merge-base contra git REAL (`merge_base.py` — nunca rebase durante review
> humano, zero threads órfãs, failure mode 11) exposto como a Activity do
> contrato `update_base_branch` (`ACTIVITY_UPDATE_BASE_BRANCH`); emissão de
> episódios de skill-learning de review feedback aceito (`review_learning.py`,
> `skill_episode` source=`review_feedback` da migração 0019 — NENHUMA skill
> criada/ativada, fronteira testada); migração `migrations/0020_wse4.sql`
> (`wse_base_updates`). **114 testes passando** (45 F1 + 26 F2 + 32 F3 + 11 F4),
> git/Postgres/Temporal/Garage/k3d+Argo CD/Playwright reais — nada mockado.

> **Fase 3 — resumo do que foi adicionado** (detalhe abaixo em §Fase 3):
> artifact store Garage real (`evidence/garage.py` + `docker-compose.wse.yml`),
> vídeo @demo Playwright real (`evidence/demo.py` + runner pinado em
> `playwright/`), previews por PR via Argo CD ApplicationSet contra o cluster
> k3d real (`preview/` + git smart HTTP próprio em `gitserver/`), L3 completo
> (`github/l3.py`), visual diff Pillow (`evidence/visual_diff.py`), publicação
> consolidada/debounced (`evidence/publication.py`), migração
> `migrations/0017_wse3.sql`. As 4 Activities do CONTRATO da Fase 3
> (`publish_artifact`, `run_demo_evidence`, `trigger_preview`,
> `run_visual_diff`) registradas em `ALL_ACTIVITIES`. **103 testes passando**
> (45 F1 + 26 F2 + 32 F3), Garage/Postgres/Temporal/k3d+Argo CD/Playwright
> reais — nada mockado para durabilidade/política.

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

## Fase 3 ("Evidence") — o que WS-E adicionou

Infra nova deste workstream (fragment `docker-compose.wse.yml`, rede `dse_net`):

- **`garage`** (`dxflrs/garage:v1.1.0`, pinado) — artifact store S3 self-hosted,
  portas reservadas 3900 (S3)/3903 (admin). Single-node dev layout; bootstrap
  idempotente por código (`evidence/garage.ensure_garage_ready`) via admin API —
  layout, chave S3 do serviço e bucket por tenant. Config em `garage/garage.toml`
  (segredos DEV-ONLY; produção = Vault/ESO, WS-F).
- **`wse-gitserver`** (imagem própria em `gitserver/`, base `alpine:3.20` pinada) —
  git **smart HTTP** (`git-http-backend` atrás de nginx+fcgiwrap) servindo o repo
  bare de manifests de preview (`preview_repo/preview-manifests.git`) ao Argo CD
  do cluster k3d. **Por quê**: o go-git do Argo CD não fala o protocolo dumb
  (nginx estático falha com `unexpected EOF` no ls-remote — visto na prática).
  Fetch-only; o host escreve por filesystem (bind mount).

### WSE-E5-T12 — Artifact store Garage

`evidence/garage.py` — Activity `publish_artifact` (nome do CONTRATO
`ACTIVITY_PUBLISH_ARTIFACT`; input/output `PublishArtifactInput`/`ArtifactRef`
importados, não redefinidos):

- **bucket por tenant** (`dse-tenant-<slug>`, NFR-03) + chave prefixada por
  WorkItem (`<wi>/<kind>/<arquivo>`); upload real via boto3; **multipart
  explícito** (create/upload_part/complete) acima de 5 MiB — validado com um
  **vídeo mp4 real >5MB gerado por ffmpeg** e conferido byte a byte no round-trip
  (obrigação do ADR-18 revisado).
- **links EXPIRAM por política** (exit da Fase 3): presigned URL com TTL do
  input; teste real prova que a URL expirada retorna **negado** (Garage nega com
  400; AWS usaria 403 — documentado no teste) e que `resolve_artifact_url`
  recusa com `PermissionError` (P6).
- **QUARENTENA** (costura EXISTENTE do WS-F, Fase 2): work item quarantinado via
  `dse_platform.kill_switches.quarantine_work_item` (`dse_work_item_quarantine`)
  => `sweep_quarantined_work_items()`/`quarantine_artifacts_for_work_item()` move
  os objetos para o prefixo `quarantine/` e **invalida o acesso antes do TTL**
  (a chave original deixa de existir — URL antiga passa a 404/403; teste real
  com TTL de 1h ainda vigente). Objeto preservado (não deletado) para auditoria.
- **LOG DE ACESSO**: toda resolução de link (`resolve_artifact_url`) grava
  `wse_artifact_access_log` (associável ao PR — insumo da métrica *evidence
  consumption*) + audit `artifact_link_resolved` (P8).

### WSE-E5-T11 — Vídeo @demo Playwright

`evidence/demo.py` — Activity `run_demo_evidence` (contrato): roda `npx
playwright test --grep @demo` REAL (runner pinado `@playwright/test 1.55.1` em
`playwright/`, Chromium instalado via `npx playwright install chromium`) com
`video: 'on'` + `trace: 'on'`, publica o vídeo (.webm) e o trace (.zip) via
`publish_artifact_core` e retorna `DemoEvidenceResult`. Vídeo verificado como
REAL (tamanho>0 + header EBML/mp4, `is_real_video`). P6: sem diretório de demo
ou sem teste `@demo` => `passed=False` com detail explícito, nunca evidência
fingida. O fixture @demo determinístico do WS-C (WSC-E3-T4b) estava em
construção paralela — o fixture local mínimo deste WS vive em
`tests/fixtures/demos/wi_demo_fixture/` (página HTML estática + spec `@demo`),
convenção de path `demos/<work_item_id>/`.

### WSE-E4-T10 — Previews por PR via Argo CD ApplicationSet (cluster k3d REAL)

`preview/paths_filter.py` + `preview/gitops.py` + `preview/argocd.py` —
Activity `trigger_preview` (contrato):

- decisão UI-touching por **paths-filter determinístico** (FR-20, fnmatch dos
  `files_changed` contra `ui_path_globs`; semântica de `**/` documentada e
  testada). Backend-only => `skipped_backend_only` (sucesso, NUNCA bloqueia).
- quando UI-touching: escreve `previews/preview-<wi>/` (Namespace + Deployment
  `nginx:1.27-alpine` pinado + Service) no repo git de manifests e o
  **ApplicationSet `dse-previews`** (generator git `previews/*`,
  `requeueAfterSeconds: 15`, `goTemplate`) do **Argo CD v2.13.3 real** cria a
  Application e sincroniza => namespace efêmero `preview-<work_item_id>` no
  cluster `k3d-dse-preview`. Teste de integração real: namespace criado, **URL
  respondendo HTTP 200** (probe curl in-cluster contra o Service), TTL
  destruindo.
- falha/timeout de provisionamento => status **`degraded`** (failure mode 9 —
  PR nunca bloqueia; testado com kubecontext inexistente).
- **caps de concorrência por tenant desde o dia 1** (ADR-26): tabela
  `wse_preview_caps` (default `DSE_PREVIEW_MAX_CONCURRENT`); no cap =>
  `degraded` com detail explícito; teste de contagem real no Postgres.
- **TTL reaper — decisão documentada**: o adendo prefere kube-janitor (P7), mas
  com Argo CD em `automated.selfHeal` a fonte de verdade é o GIT — kube-janitor
  deletaria o namespace e o Argo CD o RECRIARIA (dois controllers brigando).
  O reaper correto em GitOps é `reap_expired_previews()` (job Python
  determinístico, real): remove o diretório do repo, o ApplicationSet poda a
  Application e o finalizer `resources-finalizer` cascateia a deleção do
  namespace (provado no teste e2e). kube-janitor fica como upgrade path para
  recursos não-GitOps; a annotation `janitor/ttl` já é gravada no Namespace.

### WSE-E4-T9b — L3 completo

`github/l3.py` — `consume_ci_status_l3` (a Activity `consume_ci_status` ganhou
o campo ADITIVO `surface_ref`; payloads antigos do WS-B seguem decodificando —
boundary tests da fundação intactos):

- **reflexão** do status agregado no tracking comment único do PR (mesmo
  `MutableCommentWriter` da fundação, surface `github_pr_ci`, editado in-place)
  na MESMA chamada do consumo — <1min por construção, medido no teste;
- **targeted re-runs** em fix commit: novo sha após estado `red` => re-request
  SÓ dos check-runs falhos (`rerequest_check_run`, implementado de verdade no
  `RealGitHubClient` contra `POST .../check-runs/{id}/rerequest` e exercitado
  com `FakeGitHubClient`); CI sem suporte a re-run por job (403/422) => segue
  sem re-run, sem bloquear. Evidência em `wse_ci_reruns` + audit;
- **episódios de skill-learning** de CI-repair: transição red(sha A) ->
  green(sha B) emite episódio tenant-scoped em `wse_ci_repair_episodes` com
  proveniência (repo/PR/shas) e `occurrence_n` do padrão
  (`failure_signature` determinística). **NENHUMA skill criada/ativada**
  (conferido no teste contra `skill_registry`) — promoção é Fase 4.

### WSE-E5-T13/T14 — Visual diff + publicação debounced

- `evidence/visual_diff.py` — Activity `run_visual_diff` (contrato): pixel-diff
  **Pillow** com tolerância por canal (8/255, anti-ruído de encoding) e
  threshold percentual; **self-hosted, sem SaaS** (Argos/Percy/`toHaveScreenshot`
  = upgrade path documentado). Baseline no artifact store (kind
  `visual_baseline`, TTL 30d): primeiro run cria baseline
  (`baseline_created=true`); regressão gera imagem de diff (pixels mudados em
  vermelho) publicada como `visual_diff`. Tamanhos diferentes = 100% (mudança
  estrutural). **Pedido de campo no contrato** (regra do adendo — documentar em
  vez de editar a fundação): `VisualDiffResult` não tem campo para devolver a
  chave da baseline recém-criada; hoje ela volta em `diff_artifact_key` quando
  `baseline_created=true` (documentado aqui e na docstring) — um
  `baseline_artifact_key: str | None` dedicado seria mais limpo.
- `evidence/publication.py` — publicação CONSOLIDADA: vídeo/trace/diff/preview/
  status de CI num único tracking comment (surface `github_pr_evidence`),
  corpo re-renderizado do ESTADO DO BANCO (crash-consistente); artefato
  quarantinado aparece como revogado, nunca como link. **Debounce (ADR-26)**:
  `should_refresh_evidence()` é a decisão 100% determinística consumida pelo
  workflow do WS-B (Activity `wse_should_refresh_evidence`, retorno
  `{"refresh": bool, "reason": str}`): refresh SÓ a pedido humano explícito ou
  commit novo que muda comportamento (docs-only e mesmo-commit são debounced);
  cada decisão de debounce audita `evidence_refresh_debounced`.

### Activities novas (Fase 3) registradas no Worker único

| Nome | Input | Retorno | Contrato? |
|---|---|---|---|
| `publish_artifact` | `PublishArtifactInput` | `ArtifactRef` | SIM (`ACTIVITY_PUBLISH_ARTIFACT`) |
| `run_demo_evidence` | `RunDemoEvidenceInput` | `DemoEvidenceResult` | SIM (`ACTIVITY_RUN_DEMO_EVIDENCE`) |
| `trigger_preview` | `TriggerPreviewInput` | `PreviewRef` | SIM (`ACTIVITY_TRIGGER_PREVIEW`) |
| `run_visual_diff` | `RunVisualDiffInput` | `VisualDiffResult` | SIM (`ACTIVITY_RUN_VISUAL_DIFF`) |
| `wse_quarantine_artifacts` | `QuarantineArtifactsInput` | `list[str]` | não (aux; par do kill switch do WS-F) |
| `wse_reap_previews` | — | `list[str]` | não (aux; cron/timer do WS-B) |
| `wse_should_refresh_evidence` | `ShouldRefreshEvidenceInput` | `dict` | não (contrato de decisão ADR-26 p/ WS-B) |
| `wse_publish_evidence` | `PublishEvidenceInput` | `dict` | não (publicação consolidada) |

## Fase 4 ("Loop hardening & learning") — o que WS-E adicionou

### WSE-E6-T16 — merge-base, nunca rebase durante review (P0, CONSTRUÇÃO NOVA)

Achado #2 do adendo 03: merge-base **não existia** — a Fase 1 descreveu no
plano mas nunca implementou; o review loop só re-rodava o Coder no mesmo
branch. Construído do zero em `dse_validation/merge_base.py`, exposto como a
Activity do CONTRATO `update_base_branch` (`ACTIVITY_UPDATE_BASE_BRANCH`;
input/output `UpdateBaseBranchInput`/`UpdateBaseBranchResult` importados, não
redefinidos).

- `update_base_branch_core(...)` — quando a base (main) avança durante um review
  humano ativo, atualiza o branch da tarefa por **merge-base-into-branch**
  (`git merge origin/main` NO branch da task) — NUNCA rebase+force-push. A
  escolha de estratégia é 100% DETERMINÍSTICA (P1, código, nunca modelo):
  - sem drift → `noop_no_drift`;
  - drift + já houve review humano (`first_human_review_done=True`, o **default
    seguro** do contrato) OU já existem threads ancoradas → `merge_base`
    (preserva a história → preserva as âncoras das threads);
  - drift + ainda não houve review E zero threads ancoradas →
    `rebase_prefirst_review` (único momento em que rebase é seguro: não há o que
    orfanar; push `--force-with-lease`);
  - **belt-and-suspenders (P6)**: mesmo com `first_human_review_done=False`, se
    já existem threads ancoradas, jamais rebase — cai em `merge_base`.
- **Conflito não-resolvível** → `git merge --abort` (ou `rebase --abort`),
  retorna `conflict=True` (tip inalterado, working tree limpa). O workflow do
  WS-B escala a um humano — **o agente NUNCA resolve à força** (P1/P6).
- **A ASSERÇÃO DE EXIT DA FASE 4** (`tests/test_merge_base.py`): cria um PR com
  drift de base + 2 threads de review humanas ancoradas em commits, aplica
  merge-base, e prova `orphaned_threads == 0` comparando a alcançabilidade dos
  shas ancorados (`git merge-base --is-ancestor <sha> <branch>`) — merge
  preserva, rebase quebraria. Um **teste negativo** (`test_rebase_would_orphan_
  threads_documented_negative`) executa um rebase real e prova que TODAS as
  threads ficariam órfãs — documentando por que merge-base é obrigatório.
- **NÃO quebra a invariante anti-merge-AUTOMÁTICO (FR-16)**: merge-base atualiza
  o BRANCH DA TAREFA com o drift da base (`origin/main → branch`); o merge do PR
  na base continua 100% humano. São operações opostas em direção.
- Evidência durável (P8): `wse_base_updates` (`migrations/0020_wse4.sql`) +
  audit `base_branch_updated` / `base_update_conflict`.

### WSE-E6-T18 — Emissão de episódios de skill-learning (review feedback)

`dse_validation/review_learning.py` — 3ª "source at launch" de episódios
(§10.17), par das de CI-repair (Fase 3) e clarificação (WS-B). Quando um
feedback de review humano é ACEITO, grava um `skill_episode`
(`source='review_feedback'`, tabela da migração 0019/WS-C — SÓ INSERT/SELECT):

- `review_pattern_key(comment_body, path)` — assinatura DETERMINÍSTICA (P1):
  normalização de string (lower + colapsa espaços) escopada pelo path, hash
  curto estável — nenhum LLM. Feedbacks com o mesmo texto normalizado no mesmo
  path colidem de propósito (é o "mesmo padrão repetido").
- `record_review_feedback_episode(...)` — `occurrence_n` conta as repetições do
  MESMO `(tenant, source, pattern_key)` (tenant-scoped); proveniência completa
  (PR, reviewer, path, comentário, diff_hunk). **P3**: só feedback ACEITO por um
  humano vira episódio (`accepted=False` → nada). Audit
  `review_feedback_episode_recorded` (P8).
- **FRONTEIRA testada** (`tests/test_review_learning.py::test_boundary_no_skill_
  created_or_activated`): gravar o episódio NÃO cria/ativa nenhuma skill
  (`skill_registry` inalterado antes/depois). A promoção candidate→eval→
  approved→canary→active é 100% do **WS-C** (WSC-E4-T2/T3), com aprovação humana
  (P3: nenhuma skill se auto-promove). WS-C consome estes episódios.
- Exposto como a Activity auxiliar `wse_record_review_episode`
  (`RecordReviewEpisodeInput`; prefixo `wse_`, não-contratual).

### Fixture / real / gap — Fase 4

| Componente | REAL nesta sessão | Fixture | Gap p/ produção |
|---|---|---|---|
| merge-base | **git real** (bare repo local + clones), merge/rebase/abort/push reais, alcançabilidade de sha real; Postgres real (`wse_base_updates`) + audit | — | O **wrapper de Activity** (`_update_base_branch`) resolve o workspace via `MergeBaseConfig` (env `DSE_WSE_GIT_ROOT`) e as threads ancoradas via `list_review_threads` do GitHub client — seam de integração com o workspace do sandbox do WS-C + a GitHub App real (mesma pendência das Fases 1-3: sem App registrada, `FakeGitHubClient.list_review_threads` é fixture). O core é chamado direto pelos testes com paths explícitos (como o `LocalFakeSandbox` no L1). |
| episódios de review-feedback | Postgres real (`skill_episode`), occurrence_n/tenant-scope/audit reais | O feedback aceito é fornecido pelo caller (WS-B decide "aceito" a partir do review humano) | Fio de disparo no workflow do WS-B: chamar `wse_record_review_episode` quando um `changes_requested` é endereçado e o revisor aceita. Correlação de "aceite" é do WS-B/WS-A. |

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

## Fixture / real / gap — Fase 3 (honestidade sobre o que foi exercitado)

| Componente | REAL nesta sessão | Fixture | Gap p/ produção |
|---|---|---|---|
| Garage (S3) | Container real `dxflrs/garage:v1.1.0`, upload/presign/multipart/copy/delete reais, política de expiração provada com relógio real | — | Segredos (rpc_secret/admin_token) são dev-only no repo; produção injeta via Vault/ESO (WS-F). Single-node; multi-nó é config |
| Playwright @demo | Execução real (`npx playwright test --grep @demo`), Chromium real, vídeo webm + trace zip reais publicados no Garage | A PÁGINA demo é o fixture local mínimo (`tests/fixtures/demos/wi_demo_fixture/`) — o WS-C entrega o fixture oficial em paralelo | Rodar os @demo DENTRO do sandbox do WS-C (Playwright na imagem do sandbox, WSC-E3-T4b) em vez do host; `base_url` de preview real já é suportado no input |
| Previews Argo CD | Cluster k3d real, Argo CD v2.13.3 real, ApplicationSet real, namespace criado, URL 200 provada, TTL reap destruindo namespace de verdade | — | `PreviewRef.url` é o DNS in-cluster (`*.svc.cluster.local`) — exposição externa (ingress/port-forward gerenciado) não implementada; imagem do preview é nginx estático, não o build do PR (precisa do pipeline de imagem do repo alvo) |
| L3 (reflexão/re-runs/episódios) | Postgres real, comment store real, lógica completa | `FakeGitHubClient` (mesma interface; `RealGitHubClient.rerequest_check_run` implementado contra a API real mas sem GitHub App registrada nesta sessão) | Mesma pendência das Fases 1-2: exercitar contra `api.github.com` real |
| Visual diff | Pillow real, baseline round-trip real no Garage | Screenshots dos testes são PNGs gerados (não capturas de browser) — a captura em si é do fluxo @demo/preview | Integrar captura de screenshot do Playwright ao fluxo (hoje o caller fornece o PNG candidato) |
| Publicação/debounce | Postgres real, render do estado real, debounce provado | `FakeGitHubClient` p/ o comentário | Idem GitHub App real |
| Quarentena | Costura real com `dse_platform` (WS-F) — tabela e função da Fase 2, audit dos dois lados | — | Disparo automático (hoje `sweep_quarantined_work_items()`/Activity é chamado por quem quarantina ou por cron; falta o hook do WS-F chamar o sweep no próprio `quarantine_work_item`, decisão dele) |

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

`migrations/0020_wse4.sql` (Fase 4, reservada para WS-E) adiciona:
`wse_base_updates` (evidência de cada merge-base/rebase: estratégia, conflito,
`orphaned_threads`, shas antes/depois). Os episódios de review-feedback usam a
`skill_episode` (`source='review_feedback'`) da migração 0019 (WS-C) — WS-E só
faz INSERT/SELECT (grant da 0019), nenhuma tabela nova para isso.

`migrations/0017_wse3.sql` (Fase 3, reservada para WS-E) adiciona:
`wse_artifacts` (registro de artefatos publicados + estado de quarentena),
`wse_artifact_access_log` (log de acesso associável ao PR — métrica evidence
consumption), `wse_previews` (estado/TTL dos previews), `wse_preview_caps`
(caps ADR-26 por tenant), `wse_ci_reruns` (targeted re-runs),
`wse_ci_repair_episodes` (episódios de skill-learning tenant-scoped) e
`wse_evidence_publications` (estado do debounce ADR-26).

## Como rodar os testes

```bash
python3.12 -m venv /Users/saraiva/Documents/DSE/fase1/.venv-wse
source /Users/saraiva/Documents/DSE/fase1/.venv-wse/bin/activate
pip install -e /Users/saraiva/Documents/DSE/fase1/packages/contracts \
            -e /Users/saraiva/Documents/DSE/fase1/packages/dse_audit \
            -e /Users/saraiva/Documents/DSE/fase1/packages/dse_identity
pip install -e /Users/saraiva/Documents/DSE/fase1/services/validation
pip install -e /Users/saraiva/Documents/DSE/fase1/services/platform  # Fase 3: costura de quarentena (WS-F)
pip install pytest pytest-asyncio ruff mypy   # ruff/mypy só para os testes de L1 exercitarem os defaults reais

# Fase 3 — runner Playwright pinado + browser (uma vez):
(cd /Users/saraiva/Documents/DSE/fase1/services/validation/playwright && npm install && npx playwright install chromium)

pytest -q /Users/saraiva/Documents/DSE/fase1/services/validation
```

Requer a infra da fundação no ar (Postgres em `localhost:5432` com
`migrations/0001_foundation.sql` E `0006_wse.sql` aplicadas, Temporal em
`localhost:7233`) — os testes de idempotência de PR e de audit usam Postgres
real, e os testes de `review_signal` usam Temporal real (nunca mockados,
por design: são as garantias de durabilidade/idempotência do próprio sistema).
**Fase 3 requer ainda**: `0017_wse3.sql` aplicada; Garage + wse-gitserver no ar
(`docker compose -f docker-compose.wse.yml up -d --build`); o cluster k3d
`dse-preview` com Argo CD (fundação, `infra/k8s-local/setup-k3d-argocd.sh`);
`ffmpeg` no host (fixture de vídeo >5MB do teste de multipart). O teste e2e de
preview (`test_preview_e2e_real_cluster_create_serve_and_ttl_reap`) leva
~4-5min (sync do Argo CD + cascade delete reais).

## Resultado real da última execução

```
114 passed in ~274s   (45 Fase 1 + 26 Fase 2 + 32 Fase 3 + 11 Fase 4)
```

Fase 4 adicionou `tests/test_merge_base.py` (6 — inclui a asserção de exit
"zero threads órfãs" + o teste negativo que prova que rebase quebraria) e
`tests/test_review_learning.py` (5) — git real + Postgres real; audit rows de
`base_branch_updated`/`base_update_conflict`/`review_feedback_episode_recorded`
conferidos no `audit_log` real (P8). A fronteira "nenhuma skill criada/ativada"
é verificada contra `skill_registry` real. Nenhum teste mockado/skipado. Os
boundary tests da fundação (`test_activity_boundaries.py`, agora 15 com os 4 da
Fase 4) seguem passando SEM alteração — nenhum call site do workflow foi mudado
por este WS (a Activity `update_base_branch` usa exatamente o
`UpdateBaseBranchInput`/`Result` do contrato).

Fase 3 adicionou `tests/test_artifact_store.py` (5), `tests/test_demo_evidence.py`
(3), `tests/test_trigger_preview.py` (8, inclui o e2e real contra o k3d),
`tests/test_l3.py` (6), `tests/test_visual_diff.py` (5) e
`tests/test_evidence_publication.py` (5) — Garage/Postgres/k3d+Argo CD/
Playwright/ffmpeg reais; GitHub via `FakeGitHubClient` (mesma interface do
Real, ver tabela fixture/real/gap). Audit rows de `artifact_published`/
`artifact_link_resolved`/`artifact_quarantined`/`demo_evidence_run`/
`preview_created`/`preview_reaped`/`ci_status_reflected`/`ci_targeted_rerun`/
`ci_repair_episode_recorded`/`evidence_published`/`evidence_refresh_debounced`
conferidos no `audit_log` real (P8). Nenhum teste skipado. Os testes de
boundary da fundação (`packages/contracts/tests/test_activity_boundaries.py`,
11) seguem passando sem alteração — nenhum call site do workflow foi mudado
por este WS (o campo novo `surface_ref` de `ConsumeCiStatusInput` é aditivo e
opcional, model do próprio WS-E).

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

Fase 3 (novas):

6. **`VisualDiffResult` sem campo para a chave da baseline criada** — pedido de
   campo novo no contrato (`baseline_artifact_key`); enquanto isso a chave volta
   em `diff_artifact_key` quando `baseline_created=true` (documentado).
7. **@demo roda no host, não dentro do sandbox do WS-C** — quando a imagem do
   sandbox tiver Playwright (WSC-E3-T4b), `run_demo_evidence_core` ganha um
   caminho via `SandboxExecutor` (o input do contrato já carrega `sandbox`).
8. **URL de preview é in-cluster** (`*.svc.cluster.local`) — exposição externa
   (ingress) e imagem de preview construída do PR (em vez de nginx estático)
   ficam para a integração com o pipeline de build do repo alvo.
9. **Reaper de previews é chamado sob demanda** (Activity `wse_reap_previews`)
   — falta o WS-B agendar o timer/cron durável; a annotation `janitor/ttl` já
   permite migrar para kube-janitor em recursos não-GitOps.
10. **Fixture @demo oficial do WS-C** em construção paralela — quando publicar
    `demos/<wi>/` no repo alvo, os testes deste WS podem apontar para lá (o
    fixture local mínimo continua como fallback documentado).
