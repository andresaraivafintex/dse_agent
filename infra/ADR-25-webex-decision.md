# ADR-25 — Cisco Webex como superfície de intake (Fintex DSE)

Status: **Accepted — DE-SCOPE FORMAL (Webex fora do escopo do piloto)**. Fase 4. Autor: WS-F.
Decisão de go/no-go executada conforme adendo 03 §Parte 2 #6 e §Parte 3 ("executar como restaurar
atrás do adapter OU de-scope formal registrado").

Nada é descartado silenciosamente — este ADR é o registro explícito exigido pelo espírito da
disciplina de ADR: a decisão, a justificativa, e **exatamente o que reverter custaria**.

## Contexto

O DSE recebe pedidos de trabalho por superfícies de chat/ticket normalizadas em um
`ConversationEvent` (`dse_contracts.conversation_event`). A interface de adapter já está **provada
e madura**: três adapters em produção como molde — `adapter-slack`, `adapter-github`,
`adapter-jira` — todos com o mesmo formato (inbound com verificação de assinatura → sanitização →
`ConversationEvent`; outbound com "exatamente 1 comentário de status mutável por surface" via
`dse_contracts.mutable_comment.MutableCommentWriter`).

Webex apareceu no planejamento inicial como uma quarta superfície possível de chat. A pergunta de
go/no-go: **construir o `adapter-webex` agora, ou de-scope formal?**

Fatos que pesam na decisão (verificados, não assumidos):

1. **Teams é a provisão de chat priorizada**, não Webex (adendo 03 §Parte 2 #7 lista o Teams
   adapter como o escopo órfão de chat que recebe esforço; Webex não).
2. A interface de adapter é um **molde estável** — adicionar uma superfície é mecânico, mas cada
   adapter novo carrega custo real e recorrente: registro/rotação de segredo de webhook, um
   esquema de assinatura próprio para atacar no red-team (`test_red_team.py::TestForgedWebhook`
   hoje cobre Slack/GitHub; um novo precisaria de sua própria linha), um back-end de
   `MutableCommentWriter`, mapeamento de tenant (`tenant_platform_bindings`), e uma credencial de
   app real (item de maior lead time — adendo 03 §Parte 3).
3. **Nenhum cliente do piloto pediu Webex.** As superfícies exigidas pelos pilotos em vista são
   Slack e/ou GitHub e/ou Jira, com Teams como próxima provisão.
4. Construir um adapter sem um consumidor real viola boring-first (P7): é superfície de ataque e
   custo de manutenção sem demanda que o justifique.

## Decisão

**De-scope formal do Webex do escopo de engenharia do piloto.** Não construir `adapter-webex`
nesta fase nem antes do go-live do piloto. O esforço de adapter de chat disponível vai para
**Teams** (a provisão priorizada).

Isto é uma decisão de **negócio/priorização**, não uma limitação técnica: a interface está pronta,
o molde existe, e restaurar Webex é mecânico (ver "Como reverter" abaixo). O de-scope é sobre
**não gastar esforço agora**, não sobre incapacidade.

### Consequências

- O contrato `ConversationEvent` e a interface de adapter **permanecem agnósticos de superfície** —
  nada nelas presume o conjunto {Slack, GitHub, Jira, Teams}. Adicionar Webex depois não exige
  mudança de contrato (mudança aditiva de um novo `platform` value, como foi Jira e será Teams).
- O `platform` enum / roteamento não ganha um valor `webex` agora (não introduzimos código morto).
- O threat model (`infra/THREAT-MODEL.md §2.1`) e o red-team (`infra/RED-TEAM-PROGRAM.md §3`)
  cobrem "adapters" genericamente; quando/se Webex entrar, ganha sua própria linha de ataque de
  assinatura na suíte — item já previsto no procedimento (§ "ad-hoc: a cada mudança em um
  controle de segurança, o autor adiciona o ataque correspondente").

## Alternativa considerada e rejeitada: restaurar atrás do adapter agora

Construir `adapter-webex` imediatamente aproveitando o molde. **Rejeitada** porque:
- Sem consumidor (fato #3), é custo e superfície de ataque sem retorno (fato #4, P7).
- Compete por esforço com Teams, que **tem** demanda priorizada (fatos #1).
- O lead time real de um adapter não é o código (mecânico) e sim a **credencial de app real** +
  o registro de webhook — que só faz sentido puxar quando um cliente concreto usa Webex.

## Como reverter (o que seria necessário para restaurar) — nada é descartado

Se um cliente exigir Webex, restaurar é **mecânico** e estimável, seguindo o molde dos três
adapters existentes:

1. **`services/adapter-webex/`** espelhando `adapter-jira/` (o mais recente): inbound handler que
   (a) verifica a assinatura do webhook Webex (HMAC-SHA1/SHA256 sobre o corpo com o segredo do
   registro — o mesmo padrão puro de `ingest_gateway/security.py`), (b) sanitiza
   (`sanitize_content`), (c) emite `ConversationEvent` com `platform="webex"`.
2. **Outbound** via um novo back-end de `MutableCommentWriter` (Webex Messages API) — "exatamente
   1 mensagem de status mutável por surface", idêntico ao contrato dos outros.
3. **Mapeamento de tenant**: uma linha de binding em `tenant_platform_bindings` (migração 0008) —
   sem migração nova.
4. **Segurança**: registrar o app Webex real + segredo de webhook no Vault; adicionar a linha de
   ataque de assinatura forjada em `services/platform/tests/test_red_team.py::TestForgedWebhook`;
   se o deployment for topologia B/air-gapped, avaliar se Webex (SaaS externo) é sequer admissível
   (provavelmente exige exceção explícita revisada pelo red-team, §4 do RED-TEAM-PROGRAM.md).
5. **Roteamento**: adicionar `webex` ao roteamento de signal por status do ingest (aditivo).

Estimativa de referência: comparável ao adapter Teams / a um adapter novo do molde (≈3 pw, a
mesma ordem do escopo órfão de chat do adendo 03 §Parte 2 #7). Nenhum trabalho de plataforma
(contrato, isolamento, audit) precisa ser refeito — só o adapter e seu registro de credencial.

## Sign-off

| Papel | Decisão | Nota |
|---|---|---|
| Autor (WS-F, plataforma/segurança) | **De-scope formal aprovado** | Registrado neste ADR; reversão documentada e mecânica. |
| Arquiteto | **Requer assinatura** | Consistente com Teams-priorizado (adendo 03) e P7 (boring-first). |
| Stakeholder do piloto | **Requer assinatura** | Confirmar que nenhum cliente do piloto usa Webex como superfície primária. |

Enquanto as assinaturas de arquiteto/stakeholder não forem coletadas, o status operacional é
"de-scope proposto por WS-F, pendente de ratificação" — mas a decisão de engenharia (não construir
agora) já vale, pois é reversível a custo conhecido e não bloqueia nada.
