# Convenções do monorepo — Fintex DSE, Fase 1 (Core loop)

Lido por: todo agente/engenheiro que for construir dentro de `services/*`. Este documento é o
"contracts sprint" das semanas 1-2 do plano mestre (D1, D2, D3, D5, D6, D7, D10, D11, D12):
define o que já está construído (fundação) e as regras para não colidir com os outros
workstreams enquanto trabalham em paralelo.

## Escopo desta Fase 1 ("Core loop")

Conforme `plano-desenvolvimento/00-PLANO-MESTRE.md` §3: o ciclo completo Slack/GitHub →
clarificação → **Coder único** em sandbox → L1 → PR determinístico → review humano → merge
humano, durável e auditável. **Não fazem parte da Fase 1**: split Planner/Tester/Reviewer
(Fase 2), gate de aprovação de plano por risk class (Fase 2), Jira (Fase 2), L2 fresh-context
review (Fase 2), previews Argo CD / evidência Playwright (Fase 3), skill registry (Fases 2/4).

## Stack

- **Linguagem: Python 3.11+** em todos os serviços (containers usam `python:3.11-slim`; não
  depende da versão do host). Motivo: OpenHands SDK e LiteLLM são nativos em Python — minimiza
  atrito de integração entre o runtime de sandbox e o model gateway.
- Empacotamento: `pyproject.toml` (PEP 621) + `setuptools`, sem Poetry/uv (P7 — boring-first,
  menos uma dependência de tooling).
- Validação de dados: **pydantic v2**.
- Orquestração durável: **Temporal** (Python SDK), self-hosted via docker-compose local.
- Banco: **Postgres 16**. Migrações SQL puras e numeradas (ver abaixo), aplicadas por
  `make migrate` (script simples em `scripts/migrate.py`, sem framework de migration).
- Testes: **pytest**, com fixtures que sobem contra o Postgres/Temporal do `docker-compose.yml`
  (nunca mocks para as garantias de durabilidade/idempotência — são o próprio ponto do sistema).
- HTTP: **FastAPI** para qualquer serviço que recebe webhooks.

## Propriedade de diretórios (nenhum agente edita fora do seu escopo)

| Diretório | Workstream | Conteúdo |
|---|---|---|
| `packages/contracts/` | Fundação (não editar sem avisar o arquiteto) | `ConversationEvent`, `WorkItem`/`DseTaskRequest`/`DseTaskStatus`, `PlanArtifact` (stub), contrato de consumo do gateway, biblioteca de comentário mutável único por surface |
| `packages/dse_audit/` | Fundação (mínimo) → **WS-F estende** | Cliente de escrita do audit ledger + queries de reconstrução/export |
| `packages/dse_identity/` | Fundação (mínimo) → **WS-F estende na Fase 2** | Resolução `platform_user_id` → principal único |
| `services/adapter-slack/` | **WS-A** | Inbound (menções/replies/botões) + outbound (status message única editada in-place) |
| `services/adapter-github/` | **WS-A** | Inbound (issues/PR comments) + outbound (status comment único) via GitHub App |
| `services/ingest-gateway/` | **WS-A** | Gateway transacional (outbox), dispatcher (`SELECT…FOR UPDATE SKIP LOCKED` → `StartWorkflow`), 4 defesas (assinatura, TOCTOU snapshot, sanitização, idempotência), correlação Path A/B, steering allowlist fallback |
| `services/orchestrator/` | **WS-B** | Worker Temporal, workflow da máquina de estados §9.3, pause points, budgets, checkpoint/recovery, controles de operador, suíte de chaos |
| `services/sandbox-runtime/` | **WS-C** | Lifecycle do sandbox (provision/teardown/checkpoint) como Activities, driver Docker rootless, interface de substrato + adapter OpenHands, sessão Coder |
| `services/egress-proxy/` | **WS-C** (WS-F assina o aceite de política) | Proxy default-deny + injeção de credenciais efêmeras |
| `services/model-gateway/` | **WS-D** | Config LiteLLM, tier Bedrock/PrivateLink como allowlist entry, virtual keys por tenant/task/stage, contrato de consumo |
| `services/validation/` | **WS-E** | Pipeline L1 (lint/typecheck/test/build + SAST/secret-scan + diff-budget/forbidden-paths), PR finalizer idempotente, consumo mínimo de status checks (L3 fatiado) |
| `services/platform/` | **WS-F** | Wiring Vault/ESO, IaC skeleton (`infra/`), parâmetros de fairness/budget por tenant, scaffolding da suíte de isolamento, observabilidade |

**Regra de ouro:** cada workstream só cria/edita arquivos dentro do seu próprio diretório
(mais o arquivo de migração e o fragment de docker-compose reservados abaixo). Se precisar de
algo em `packages/contracts` que não existe, adicione um campo/tipo novo sem remover ou renomear
o que já existe — funções e classes públicas listadas neste documento são um contrato estável.

## Higiene de commits — uma frente = um commit

Regras nascidas do sprint de fatiamento (plano 09, 2026-07-23), quando ~2.900
linhas de 6 frentes distintas se acumularam num único working tree:

- **Uma frente de trabalho = um commit** (feature, correção operacional,
  hardening de infra, i18n — cada uma separada). Revert e `git bisect` são
  parte do desenho do sistema, não luxo.
- **Arquivo que o CI referencia NUNCA fica untracked**: se `ci.yml`, o chart
  Helm ou a matriz de testes apontam para um arquivo, ele entra no MESMO
  commit que criou a referência (um clone limpo tem que passar no CI sempre).
- **Fixture gerada/mutada por teste nunca é rastreada** — regenere via código
  idempotente (padrão `ensure_repo` do preview gitops) e ignore o diretório.
- Mudança cosmética (i18n, rename em massa) nunca no mesmo commit que mudança
  de comportamento.

## Migrações — numeração reservada (evita colisão em paralelo)

`migrations/0001_foundation.sql` já existe (work_items, ingest_events, audit_log particionado
append-only, principals/identity_links). Se seu workstream precisar de tabela própria na Fase 1,
use exclusivamente o arquivo abaixo (não edite o 0001):

| Arquivo | Workstream |
|---|---|
| `migrations/0002_wsa.sql` | WS-A |
| `migrations/0003_wsb.sql` | WS-B |
| `migrations/0004_wsc.sql` | WS-C |
| `migrations/0005_wsd.sql` | WS-D (ex.: tabela de virtual keys emitidas) |
| `migrations/0006_wse.sql` | WS-E (ex.: tabela de validation runs) |
| `migrations/0007_wsf.sql` | WS-F (ex.: tenant_config — budgets/fairness/kill switches) |

Rode `make migrate` para aplicar todas as migrações em ordem (idempotente — usa uma tabela
`schema_migrations` para não reaplicar).

**Numeração (plano 09, Fase 4):** prefixo numérico é ÚNICO — o CI falha em
colisão nova (`tests/test_ci_tooling.py::test_migration_numeric_prefixes_are_unique`).
A colisão histórica `0020_wsc4`/`0020_wse4` está congelada (já aplicada em
ambientes reais; nunca renumere migração aplicada). Antes de criar uma
migração, use o menor número livre acima do maior existente.

## docker-compose — cada workstream escreve seu próprio fragment

`docker-compose.yml` (fundação) já sobe: `postgres`, `temporal` (+ `temporal-ui`), `redis`,
`vault` (dev mode). **Não edite este arquivo.** Se seu serviço precisa rodar em container,
crie `docker-compose.wsX.yml` (ex.: `docker-compose.wsa.yml`) com apenas os seus serviços,
conectados à rede externa `dse_net` (já declarada na fundação). O `Makefile` já faz o merge de
todos os fragments existentes em `make up`.

Portas reservadas (evite conflito):

| Porta | Serviço |
|---|---|
| 5432 | Postgres |
| 7233 / 8088 | Temporal frontend / Temporal UI |
| 6379 | Redis |
| 8200 | Vault (dev) |
| 4000 | LiteLLM (model-gateway, WS-D) |
| 8801 | adapter-slack (WS-A) |
| 8802 | adapter-github (WS-A) |
| 8803 | ingest-gateway (WS-A) |
| 8805 | sandbox-runtime control API (WS-C, se exposta) |
| 8806 | egress-proxy (WS-C) |
| 8807 | validation / PR finalizer webhook receiver (WS-E, se exposto) |
| 8900 | orchestrator health endpoint (WS-B) |

## Contratos já publicados (não reinvente — importe)

- `dse_contracts.conversation_event.ConversationEvent` — evento normalizado único que todo
  adapter produz (FR-01/§10.2). Campos: `event_id` (sha256 platform+thread+message),
  `platform`, `kind` (`task_request|clarification_answer|approval|review_comment|steering`),
  `source_ref` (thread_ts/ticket/pr), `actor` (platform_user_id + principal resolvido),
  `content_snapshot`, `received_at`, `signature_verified`.
- `dse_contracts.work_item.WorkItem`, `DseTaskRequest`, `DseTaskStatus` — schema de §10.3 e a
  API pública (status grosseiro: `running|blocked|done|failed`) de FR-01-04 na tabela.
- `dse_contracts.plan_artifact.PlanArtifact` — stub do artefato de plano (steps, files,
  diff_budget, test_plan, risk_class) — usado já na Fase 1 pelo diff-budget enforcement do
  WS-E mesmo sem sessão Planner separada (o Coder da Fase 1 preenche um `PlanArtifact` mínimo
  antes de implementar).
- `dse_contracts.gateway_contract` — contrato de consumo do model-gateway: base URL única,
  headers obrigatórios (`tenant_id`, `work_item_id`, `stage`, `task_class`, `data_class`),
  formato de erro de recusa de política/budget.
- `dse_contracts.mutable_comment.MutableCommentWriter` — biblioteca compartilhada de
  "exatamente 1 comentário/mensagem de status por surface, editado in-place, crash-consistent"
  (WSA-E3-T2/E4-T2, reutilizada por WSE-E3-T7). Adapters do WS-A e o PR finalizer do WS-E
  usam a mesma classe com back-ends diferentes (Slack API / GitHub API / Jira API).
- `dse_audit.client.emit(actor, action, work_item_id, tenant_id, details)` — único caminho de
  escrita no audit ledger. Nunca escreva em `audit_log` por fora desta função.
- `dse_identity.resolve_principal(platform, platform_user_id, display_name=None)` — resolve
  (e, se necessário, cria) o principal único de um usuário visto pela primeira vez numa
  plataforma. Fase 1: resolução simples por auto-registro (sem SSO/SCIM — isso é ADR-22,
  Fase 2/WSF-E3-T3). Toda superfície deve chamar isto antes de gravar `actor` em qualquer lugar.

## Princípios não-negociáveis (P1-P8 da proposta) — todo código deve respeitar

- **P1 deterministic-or-human**: nenhuma decisão de fluxo (aprovar, mergear, abrir PR,
  transicionar estado) é tomada por um LLM. Código determinístico ou humano nomeado, sempre.
- **P3 no producer approves its own work**: nenhuma sessão de agente pode aprovar/mergear o
  próprio diff.
- **P6 decline-never-truncate**: excedeu budget/cap → falha limpa em fronteira, nunca corta
  no meio. Nunca "silenciar" um erro.
- **P8 evidence over assertion**: toda decisão consequente gera uma linha no audit ledger.

## Ambiente Python para build/teste em paralelo

A fundação já validou `packages/*` num venv em `.venv/` (Python 3.12, via
`python3.12 -m venv .venv`). **Cada workstream cria o próprio venv isolado**
(`.venv-wsa`, `.venv-wsb`, `.venv-wsc`, `.venv-wsd`, `.venv-wse`, `.venv-wsf`
na raiz do repo) para instalar suas dependências e rodar `pytest` sem
interferir em instalações concorrentes de outro workstream — 6 builds andam
em paralelo neste momento. Exemplo:

```
python3.12 -m venv .venv-wsa
source .venv-wsa/bin/activate
pip install -e ../../packages/contracts -e ../../packages/dse_audit -e ../../packages/dse_identity  # ajuste o path
pip install -e .   # o pyproject.toml do seu próprio service
pip install pytest
pytest -q
```

Infra já está no ar (`docker compose up -d` já rodou: Postgres em `localhost:5432`
com a migração `0001_foundation.sql` aplicada, Temporal em `localhost:7233`,
Redis em `localhost:6379`, Vault dev em `localhost:8200` com root token
`dse_dev_root`). Não rode `make up`/`make down` você mesmo (derrubaria a infra
para os outros workstreams rodando em paralelo) — apenas conecte nela. Se
precisar de uma tabela própria, escreva-a em `migrations/000X_wsY.sql`
(seu número reservado) e aplique você mesmo com
`DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse python3 scripts/migrate.py`
(idempotente, só aplica o que for novo).

## Fase 2 ("Judgment & queue") — escopo e reservas

A Fase 1 está completa e integrada (ver `docs/PHASE1-STATUS.md` e o adendo
`../plano-desenvolvimento/01-ADENDO-FASE2-POS-FASE1.md`). A Fase 2 adiciona:
split Planner/Tester/Reviewer (WS-C), gate de aprovação de plano por risk
class + rejection path + budgets (WS-B), adapter Jira + mapeamento de tenant +
webhook de merge + roteamento de signal por status (WS-A), policy/budget no
call time + kill switch de gateway (WS-D), L2 fresh-context (WS-E), access
bundles + ADR-22/SSO design + suíte de isolamento multi-tenant + queue board
(WS-F). **Continua fora de escopo até a Fase 3/4:** previews Argo CD,
evidência Playwright/vídeo, artifact store, promoção de skills (só bootstrap
do registry na Fase 2).

Contratos novos já publicados na fundação (importe, não redefina):
`SIGNAL_PLAN_APPROVAL` (payload documentado em `constants.py`),
`ACTIVITY_RUN_PLANNER_TURN` / `ACTIVITY_RUN_TESTER_TURN` /
`ACTIVITY_RUN_L2_REVIEW`, `L2Verdict`, `PrRef.compare_url` (opcional,
`pr_number` agora opcional — exatamente um dos dois presente),
`OTEL_ATTR_TASK_CLASS`.

Migrações reservadas da Fase 2 (mesma regra da Fase 1 — um arquivo por WS):

| Arquivo | Workstream |
|---|---|
| `migrations/0008_wsa2.sql` | WS-A (ex.: tenant_platform_bindings) |
| `migrations/0009_wsb2.sql` | WS-B |
| `migrations/0010_wsc2.sql` | WS-C (ex.: skill_registry, retrieval index) |
| `migrations/0011_wsd2.sql` | WS-D (ex.: model_policies) |
| `migrations/0012_wse2.sql` | WS-E |
| `migrations/0013_wsf2.sql` | WS-F (ex.: dse_access_bundles) |

Portas novas reservadas: **8890** = queue board do admin console (WS-F).

Nota de infra: o cluster Temporal da fundação foi atualizado de
`auto-setup:1.24` para a maior versão disponível no registro (drill de
upgrade WSB-E1-T5). Priority & Fairness nativo (1.31+) NÃO está disponível —
fairness na Fase 2 é worker-side (caps de concorrência por tenant lidos de
`tenant_config`), atrás de interface trocável quando o servidor suportar.

## Fase 3 ("Evidence") — escopo e reservas

Fases 1+2 completas e integradas (399 testes — ver `docs/PHASE2-STATUS.md` e o adendo
`../plano-desenvolvimento/02-ADENDO-FASE3-POS-FASE2.md`). A Fase 3 adiciona: L3 completo
(reflection + targeted re-runs), preview environments por PR via Argo CD ApplicationSet,
vídeo `@demo` Playwright, artifact store Garage (links expirantes + quarentena + log de
acesso), visual diff, debounce de evidência (ADR-26), Playwright na imagem do sandbox +
convenção `demos/<workitem-id>/` (WSC-E3-T4b), segundo substrato (Claude Agent SDK),
failover intra-tier + bateria completa de chaos, ADR-28 completo (rotação agendada +
secrets de preview via ESO), retenção por classificação. **Fora de escopo até a Fase 4:**
promoção de skills, merge-base hardening, red-team.

**Gate de entrada JÁ EXECUTADO pela fundação:**
- Models de sessão (Planner/Tester/L2) PROMOVIDOS a `dse_contracts.activities`
  (`sandbox_runtime.activities` re-importa) com testes de regressão de boundary em
  `packages/contracts/tests/test_activity_boundaries.py` — **regra nova: ao mudar um call
  site no workflow, atualize o payload correspondente nesses testes NO MESMO PR.**
  `RunL2ReviewInput` agora tem `extra="forbid"` (P3 estrutural no decode).
- Contratos de evidência da Fase 3 JÁ DEFINIDOS na fundação (importe, não redefina):
  `ACTIVITY_RUN_DEMO_EVIDENCE`/`PUBLISH_ARTIFACT`/`TRIGGER_PREVIEW`/`RUN_VISUAL_DIFF` +
  models `RunDemoEvidenceInput`/`DemoEvidenceResult`/`PublishArtifactInput`/`ArtifactRef`/
  `TriggerPreviewInput`/`PreviewRef`/`RunVisualDiffInput`/`VisualDiffResult`.
- **Cluster K8s local no ar**: k3d `dse-preview` (2 nós, rede `dse_net` — pods alcançam
  Vault/model-gateway/Garage pelo nome de container) com **Argo CD v2.13.3** instalado e
  Available no namespace `argocd` (ApplicationSet controller incluído). Setup idempotente:
  `infra/k8s-local/setup-k3d-argocd.sh`. kubecontext: `k3d-dse-preview`. ESO é instalado
  pelo WS-F via helm (comando no rodapé do script). NÃO delete o cluster.

Migrações reservadas da Fase 3: `0014_wsb3.sql`, `0015_wsc3.sql`, `0016_wsd3.sql`,
`0017_wse3.sql`, `0018_wsf3.sql` (WS-A não tem tarefas na Fase 3).

Portas novas reservadas: **3900/3903** = Garage S3 API/admin (WS-E declara o serviço no
`docker-compose.wse.yml` — fragment ainda não existe, criar); **8091** = port-forward do
Argo CD UI (sob demanda, não fixo).

## Como rodar localmente

```
make up        # sobe infra (postgres, temporal, redis, vault) + fragments de cada workstream
make migrate   # aplica todas as migrações em ordem
make test      # roda a suíte de testes de todos os serviços (requer `make up` rodando)
make down      # derruba tudo
```
