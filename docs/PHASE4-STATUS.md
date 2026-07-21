# Fase 4 ("Loop hardening & learning") — Status da implementação

Data: 2026-07-21. Última fase de ENGENHARIA antes dos pilot gates. Escopo e ajustes conforme o
[adendo 03](../../plano-desenvolvimento/03-ADENDO-FASE4-POS-FASE3.md), precedido de validação
profunda do estado.

## Resumo executivo

- **597 testes passando, 0 falhando, 5 pulados** (Fase 3 fechou em 503; +94 na Fase 4) — suíte
  inteira re-executada com os DSNs corretos por workstream, contra Postgres/Temporal/Docker/
  Vault/LiteLLM/Garage **e o cluster k3d + Argo CD**.
- **Zero bugs de integração de contrato pela segunda fase seguida** — o gate de entrada
  (contratos + boundary tests + `extra="forbid"`) continua pagando: as 3 activities novas
  (`update_base_branch`, `eval_skill_candidate`, `promote_skill`) e os 26 nomes cross-workstream
  registraram sem colisão; worker sobe com **36 activities**.
- **Toda a engenharia da Fase 4 está entregue e provada.** O que separa o produto do piloto
  agora é **administrativo/de negócio**, não código — ver §"Pilot-readiness".

## O que foi construído (real, por workstream)

| WS | Fase 4 entregue | Prova real |
|---|---|---|
| E | **merge-base (construção nova)** — atualiza o branch da tarefa com drift da base por merge, nunca rebase depois do 1º review; conflito → escala; episódios de review-feedback | teste de exit: PR com drift + 2 threads ancoradas → merge-base → **orphaned_threads==0** (alcançabilidade real de shas); teste negativo prova que rebase orfanaria todas |
| C | **esteira de promoção de skill** candidate→eval→approved→canary→active + rollback por ponteiro; captura de episódios (3 sources) | exit: pipeline completo + rollback restaura o ponteiro (skill some do Planner); **adversarial: promote(active, approver=None) e system:* recusados** antes de qualquer escrita |
| A | **steering sobre identity map real** (RBAC do console + approvers do bundle, offboarding sobrepõe), assinatura estável da Fase 1; **adapter Teams** (provisão, não ativado) | offboarded nega apesar de allowlist; troca de impl não quebrou os testes de steering da Fase 1; Teams inbound retorna 501 até ativação |
| F | **threat model + data-flow** (ameaça→controle implementado→teste, diagramas mermaid validados); **programa de red-team** (21 ataques executáveis); **topologia B**; **decisão Webex** (de-scope formal com "como reverter") | 21/21 red-team passando contra infra real (forged webhook, prompt-injection/SSRF via egress, cross-tenant, skill maliciosa via WS-C); `helm lint`/`template` limpos em A e B |
| B | **wiring merge-base** no review loop (conflito/órfãs → escala), episódio de clarificação, 4 métricas OTel de qualidade de PR | conflito → `_EscalateNow`; orphaned_threads>0 também escala (defesa extra da invariante) |

## Exit criteria de engenharia da Fase 4 (Seção 16) — atendidos

| Critério | Status |
|---|---|
| UC4 verde incluindo asserção de zero threads de review órfãs | **Atendido** — merge-base provado com orphaned_threads==0 + wiring que escala se >0 |
| Primeira skill promovida candidate→eval→approval→canary com rollback demonstrado | **Atendido** — pipeline completo testado contra Postgres real; rollback por ponteiro |
| Decisão Webex executada (restaurar ou de-scope com sign-off) | **Atendido** — de-scope formal documentado (ADR-25), com caminho de reversão mecânico |
| Threat model + data-flow diagrams (pacote de security review) | **Atendido** — THREAT-MODEL.md com rastreabilidade ameaça→controle→teste |
| Programa de red-team antes do primeiro repo de cliente | **Atendido** — dono, cadência, 21 ataques automatizados + itens manuais |

## Pilot-readiness — a fronteira honesta (adendo 03 §3)

A Fase 4 fecha **tudo o que é engenharia**. Os pilot gates restantes **não são resolvíveis com
código** — são administrativos/de negócio e devem virar um checklist de readiness separado:

| Pilot gate (Seção 16) | Natureza | Bloqueio |
|---|---|---|
| PR quality thresholds no piloto interno | Engenharia **pronta**, dados **pendentes** | As 4 métricas OTel existem e emitem; os NÚMEROS reais exigem operar contra repos reais |
| Economics measured (Seção 15, números reais) | idem | Atribuição de custo instrumentada (Fase 2/3); números reais dependem de modelo/uso reais |
| Client security/data review passed | **Pronto para submeter** | THREAT-MODEL.md + data-flow diagrams prontos; a aprovação é do cliente |
| Licensing BOM assinado | Administrativo | `infra/OSS-BOM.md` existe; assinatura é processo |
| RACI operacional; termos contratuais executados | Negócio/jurídico | Fora de engenharia |
| Queue board demonstravelmente system of record | **Atendido** (Fase 2) | — |

**Caminho crítico para o piloto (não é código):** registrar **GitHub App / Slack / Jira / conta
AWS-Bedrock reais**. É o maior lead time pendente desde a Fase 1 e agora gateia diretamente
"PR quality thresholds" e "economics measured". **Recomendação: disparar já** — nenhuma linha de
código o desbloqueia, e todo o resto da engenharia já está pronto para consumi-lo.

## Gaps de engenharia declarados (honestos)

- **Credenciais/serviços reais** (GitHub App, Slack, Jira, AWS/Bedrock, modelo real): tudo com
  fixture/fake claramente marcado; a lógica é real contra as APIs reais. Mesmo bloqueio desde a
  Fase 1 — administrativo.
- **merge-base**: o core roda contra git real nos testes; o wrapper de Activity resolve threads
  ancoradas via GitHub client (Fake sem App). Une-se ao workspace do sandbox do WS-C na
  integração final com repo real.
- **canary = shadow** (sem seleção de subconjunto de tráfego): documentado; seleção canário real
  é evolução pós-piloto.
- **eval matcher** de skill é por `pattern_key` (determinístico, auditável, simples) — um matcher
  semântico rico é evolução futura.
- **Teams**: provisão completa e testada, **não ativada** (decisão de roadmap; Webex de-scoped).

## Como rodar

```
cd fase1
make up && make migrate
./infra/k8s-local/setup-k3d-argocd.sh && ./infra/k8s-local/setup-eso.sh
# testes por workstream, venv ATIVADO; platform/audit com DSN dse_app (nunca superuser)
```
