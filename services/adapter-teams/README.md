# adapter-teams (WS-A, Fase 4) — PROVISÃO, não ativado

Adapter Microsoft Teams espelhando `adapter-slack`/`adapter-github`/`adapter-jira`
(a estrutura desses três é o molde). Entregue **completo e testado**, porém
deliberadamente **não ativado**: ligar Teams é uma decisão de negócio/roadmap
(Fase 4+), e a ativação exige uma mudança de **fundação** que este workstream
não faz nesta sessão (convivência com 4 agentes em paralelo — não editamos
`packages/*` nem as migrações 0001-0019).

Porta reservada: **8808** (8801 slack, 8802 github, 8803 gateway, 8804 jira já
em uso; 8808 é o próximo livre do bloco WS-A).

## O que está implementado (real)

- **Inbound** (`POST /teams/messages`) — Activity do Bot Framework/outgoing
  webhook normalizada para `ConversationEvent` passando pelas **4 defesas** de
  intake, na ordem:
  1. `verify_teams_signature` (HMAC do outgoing webhook — ver abaixo). 401 + audit se falhar.
  2. `content_snapshot` congelado do próprio payload (defesa TOCTOU) — `events.clean_text`.
  3. `sanitize_content` do gateway (unicode invisível + redação de secret).
  4. idempotência: `event_id` determinístico (`events.compute_event_id`) →
     dedup em `admit_work_item`/`record_signal_event` via `UNIQUE`.
  Depois: `correlate()` decide Path A (new_task) / Path B (signal) / unauthorized
  — exatamente o mesmo caminho dos outros adapters.
- **Outbound** (`POST /internal/status-comment`) — **exatamente 1 mensagem de
  status por WorkItem, editada in-place**, via a MESMA
  `dse_contracts.mutable_comment.MutableCommentWriter` dos outros adapters, com
  `TeamsCommentBackend` (backend novo). NÃO depende da ativação (a surface é só
  a string `"teams"`) — plenamente funcional e testado já.
- **Verificação de assinatura** (`ingest_gateway.security.verify_teams_signature`):
  esquema HMAC do **Microsoft Teams outgoing webhook** — o secret é entregue em
  Base64; a cada POST o Teams envia `Authorization: HMAC <base64(HMAC_SHA256(
  decoded_secret, raw_body))>`. Verificação em tempo constante sobre o corpo
  BRUTO. (O canal Bot Framework "completo" autentica por JWT Bearer contra o
  metadata OpenID da Microsoft — documentado como passo de ativação do canal;
  o HMAC do outgoing webhook é o análogo direto de Slack/Jira e é o coberto.)
- **Transporte outbound real**: `backend.RealTeamsClient` fala o Bot Framework
  Connector REST (token client_credentials do AAD → POST/PUT de activities).
  Sem app registration/tenant real nesta sessão, `FakeTeamsClient` substitui o
  transporte nos testes — a lógica de `TeamsCommentBackend` é 100% real.

## O que falta para ATIVAR (bloqueio de fundação, decisão de negócio)

Exatamente dois passos aditivos (nenhuma mudança neste serviço):

1. **Código** — `Platform.teams = "teams"` no enum de
   `packages/contracts/dse_contracts/conversation_event.py` (1 linha aditiva).
2. **Migração** — aplicar `activation.sql` (relax aditivo dos CHECKs
   `work_items.source` e `identity_links.platform` para incluir `'teams'`).

`platform_compat.is_activated()` detecta esse estado em runtime (sem hard-code):
enquanto não ativado, `/health` reporta `{"activated": false}` e `/teams/messages`
verifica a assinatura (defesa real) e então retorna **501 `teams_not_activated`**
ANTES de qualquer escrita (evita violar os CHECKs de plataforma). Depois da
ativação, o mesmo endpoint roda o pipeline completo (`correlate`/`admit`) sem
outra mudança. Além do código, ativar em produção exige um **tenant Teams real**
+ app registration (Azure Bot) + secrets no Vault (`dse/teams/webhook`,
`dse/teams/bot`).

## Rodar os testes (infra real)

```
cd /Users/saraiva/Documents/DSE/fase1
source .venv-wsa/bin/activate
pip install -e services/adapter-teams
cd services/adapter-teams && pytest -q
```

Cobertura: `test_normalization.py` (extração + 4 defesas ao nível de função),
`test_outbound.py` (1-mensagem-por-tarefa via FakeTeamsClient + persistência
stateless), `test_inbound_pipeline.py` (corpus de forgery HMAC → 401; assinada
válida → 501 gated + audit), `test_activation.py` (o guard de ativação como
teste executável). A assinatura HMAC do Teams também tem corpus próprio em
`services/ingest-gateway/tests/test_security.py`.
