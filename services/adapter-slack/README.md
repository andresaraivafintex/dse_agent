# adapter-slack (WS-A)

Documentação completa do workstream (o que está implementado, fixtures,
o que falta para produção, pedidos ao arquiteto) está em
[`../ingest-gateway/README.md`](../ingest-gateway/README.md) — este arquivo
cobre só o que é específico deste serviço.

## Rodando localmente

```bash
source /Users/saraiva/Documents/DSE/fase1/.venv-wsa/bin/activate
pip install -e ../../packages/contracts -e ../../packages/dse_audit -e ../../packages/dse_identity \
            -e ../../services/platform -e ../../services/ingest-gateway -e .
SLACK_SIGNING_SECRET=dev_only_fixture SLACK_BOT_TOKEN=xoxb-dev-fixture \
  DSE_DATABASE_URL=postgresql://dse_app:dse_app_dev_only@localhost:5432/dse \
  uvicorn adapter_slack.app:app --port 8801
```

Endpoints:
- `POST /slack/events` — Slack Events API (`app_mention`, `message` em
  thread).
- `POST /slack/interactions` — Interactivity (`block_actions`, botões de
  approval).
- `POST /internal/status-comment` — outbound, chamado pelo orchestrator
  (WS-B) a cada transição de estado relevante.
- `GET /health`.

## Testes

```bash
cd /Users/saraiva/Documents/DSE/fase1/services/adapter-slack
pytest -q
```

Resultado desta sessão: **14 passed**. Requer Postgres real
(`localhost:5432`, migração `0002_wsa.sql` aplicada) — sem mocks de DB.
Slack em si é 100% fixture (`FakeSlackClient`) nos testes de outbound; os
testes de inbound exercitam o pipeline de assinatura/sanitização/
correlação real.
