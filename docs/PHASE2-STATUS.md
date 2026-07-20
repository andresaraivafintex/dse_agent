# Fase 2 ("Judgment & queue") — Status da implementação

Data: 2026-07-20. Escopo: implementação real da Fase 2 sobre a Fase 1 já integrada, com os
7 itens do [adendo 01](../../plano-desenvolvimento/01-ADENDO-FASE2-POS-FASE1.md) incorporados.

## Resumo executivo

- **399 testes passando, 2 pulados (com razão), 0 falhando** nos 11 pacotes/serviços, contra
  Postgres/Temporal/Docker/Vault/LiteLLM reais. (Fase 1 fechou em 223; +176 novos na Fase 2.)
- **Temporal atualizado 1.24 → 1.29** (drill WSB-E1-T5), com estado preservado (bancos próprios
  do Temporal) — validado recriando o container. Priority & Fairness nativo (1.31+) ainda não
  publicado no registro, então o fairness é **worker-side** (caps por tenant) atrás de interface
  trocável, como o adendo previu.
- **Smoke test end-to-end real** do caminho da Fase 2 executado: intake → clarificação → budget
  → **sessão Planner** → **gate de aprovação de plano** (auto-aprovado por risco baixo, com
  projeção em `plan_approval_gate`) → sandbox provisionado. O worker registra **20 Activities**
  (8 WS-C + 6 WS-E + 6 locais do WS-B).
- **5 bugs de integração reais encontrados e corrigidos** na consolidação — de novo, nenhum
  workstream os pegaria sozinho (fakes lenientes escondiam o boundary real). Ver §"Achados".

## O que foi construído (real, por workstream)

| WS | Fase 2 entregue | Testes |
|---|---|---|
| A | Adapter Jira completo (webhook + poller obrigatório + transições serializadas + transição-como-aprovação UC5), mapeamento plataforma→tenant, webhook `pull_request` merged → `merged_by_human`, roteamento de signal por status do WorkItem | 107 |
| B | Gate de aprovação de plano por risk class (com classificação de risco determinística que não rebaixa por Planner otimista), rejection path (re-plan/re-clarify/cancel), budgets na admissão e em fronteiras, fairness worker-side, sequência Planner→gate→Coder→Tester→L1→L2→PR, chaos de modelo/proxy fail-closed | 37 |
| C | Sessões Planner read-only / Tester (test-paths) / Reviewer fresh-context (P3 por construção), skill registry bootstrap (tenant-scoped), retrieval/index (BM25 + TF-IDF self-hosted, isolamento por tenant, conteúdo não confiável) | 55 |
| D | Policy per-stage/per-tenant no call time (hot-reload), enforcement de budget (decline-never-truncate), kill switch de gateway 4 escopos + reassign de modelo, custo durável ligado ao OTel collector, eval suite Tier-2 | 39 |
| E | Loop L2 fresh-context (cheapest-first, P5), fix-retries bounded L2→Coder, modo estrito de PR (humano abre, via `PrRef.compare_url`) | 71 |
| F | Access bundles por tenant/canal (cascata de aprovador vazia bloqueia), design ADR-22 + SSO OIDC real + offboarding em cascata, suíte de isolamento multi-tenant (tentativas ativas cross-tenant), admin queue board (API + controles→signals + UI mínima na 8890) | 90 |

## Achados da integração (o valor de ligar os 6 workstreams)

1. **`awaiting_plan_approval` ausente do enum de fundação** — o WS-B gravava o estado na coluna
   TEXT e a `constants.py` já o referenciava como gatilho de roteamento do WS-A, mas o enum
   `WorkItemStatus` e o `to_public_status` não o tinham. Adicionado à fundação com projeção
   pública `blocked` (§10.3).
2. **Boundary Planner quebrado** — o WS-B enviava `instructions`(lista)+`base_branch`; o
   `RunPlannerTurnInput` do WS-C exigia `instruction`(str). Fakes lenientes dos dois lados
   escondiam; o wire real crashava com "missing instruction". Modelo do Planner tornado
   tolerante (deriva `instruction` de `instructions`).
3. **Boundary Tester quebrado, nos dois sentidos** — input faltava `instruction`; e o retorno
   `TesterTurnResult` do WS-C não tinha `diff_summary`/`files_changed` que o WS-B decodifica em
   `CoderTurnResult`. Tornado superset compatível.
4. **Boundary L2 quebrado** — WS-B enviava `diff_summary`; o input estrito do WS-C exigia `diff`.
   Aqui a correção foi no **chamador** (WS-B envia `diff`), preservando o input do L2
   deliberadamente estrito — porque a prova de P3 "por construção" (o input do Reviewer tem
   exatamente `{plan, diff}`, nenhum canal para histórico do Coder) é a joia da coroa e não pode
   ser alargada. Os testes de P3 dos dois lados foram ajustados ao payload correto.
5. **Colisão potencial de nome de Activity** — WS-C e WS-E ambos tinham conceito de "L2 review".
   Verificado que o WS-E prefixou os seus (`wse_*`) e não há colisão: 14 Activities
   cross-workstream com nomes únicos, worker sobe com 20 registradas.

> Nota de dívida técnica registrada: os input/output models das Activities (Planner/Tester/L2)
> vivem em cada workstream, não em `dse_contracts` — foi o que permitiu o drift dos achados 2–4.
> A correção definitiva é promovê-los à fundação (uma fonte da verdade), agendado para a próxima
> janela de contrato do arquiteto.

## Exit criteria da Fase 2 (Seção 16) — honestamente

| Critério | Status |
|---|---|
| UC2 verde (Jira) | **Parcial** — adapter completo e testado com FakeJiraClient; falta service account/site Jira reais no Vault (operacional) |
| UC5 verde incl. block-on-unresolvable-approver | **Atendido em lógica** — gate + cascata vazia→Blocked provados por 7 testes de integração do WS-B contra Temporal real; costura do signal `plan_approval` (dispatcher→handler) verificada estaticamente e o gate auto roda ao vivo no smoke test |
| Queue board mostra todos os estados + controles com efeito real | **Atendido** — API + UI mínima (8890) + controles→signals Temporal; kill switch 4 escopos e reassign de modelo com efeito |
| Design ADR-22 fechado antes do exit | **Atendido** — `infra/ADR-22-identity.md` + SSO OIDC real + offboarding em cascata |
| Skill registry bootstrapped + retrieval operacional | **Atendido** — registry tenant-scoped com seed humano; retrieval BM25/TF-IDF com isolamento provado pela suíte cross-tenant do WS-F |

## O que falta (não escondido)

- **Caminho de alto risco end-to-end ao vivo**: o gate auto (risco baixo) roda no smoke test; o
  caminho `high → awaiting_plan_approval → signal plan_approval → prossegue` é provado por testes
  de integração (Temporal real, WS-B) mas não foi forçado end-to-end pela pilha conteinerizada,
  porque o Planner fake emite `expected_files: []` (risco baixo) e não há modelo real para
  emitir um plano de alto risco. Precisa de um modelo real ou injeção de script no Planner.
- Credenciais/instâncias reais (Slack/GitHub/Jira Apps, AWS/Bedrock) seguem pendentes — mesma
  situação da Fase 1, é bloqueio administrativo, não de engenharia.
- Promoção dos models de Activity a `dse_contracts` (dívida do achado 2–4).
- `dse_ingest_dispatcher` e demais containers: as imagens foram reconstruídas na integração; um
  redeploy limpo em produção deve rebuildar tudo (já é o fluxo do `make up`).

## Como rodar

```
cd fase1
make up && make migrate
# testes: cada workstream no seu venv ATIVADO (o L1 do WS-E precisa de ruff/mypy no PATH):
#   source .venv-wse/bin/activate && (cd services/validation && pytest -q)
```
