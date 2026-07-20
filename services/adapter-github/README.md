# adapter-github (WS-A)

Documentação completa do workstream (o que está implementado, fixtures, o
que falta para produção, pedidos ao arquiteto) está em
[`../ingest-gateway/README.md`](../ingest-gateway/README.md) — este arquivo
cobre só o que é específico deste serviço.

## Rodando localmente

```bash
source /Users/saraiva/Documents/DSE/fase1/.venv-wsa/bin/activate
pip install -e ../../packages/contracts -e ../../packages/dse_audit -e ../../packages/dse_identity \
            -e ../../services/platform -e ../../services/ingest-gateway -e .
GITHUB_WEBHOOK_SECRET=dev_only_fixture GITHUB_BOT_LOGIN=dse-bot GITHUB_TASK_LABEL=dse \
  DSE_DATABASE_URL=postgresql://dse_app:dse_app_dev_only@localhost:5432/dse \
  uvicorn adapter_github.app:app --port 8802
```

Endpoints:
- `POST /github/webhook` — webhooks da GitHub App (`issues`,
  `issue_comment`, `pull_request_review_comment`, e **Fase 2**
  `pull_request` closed/merged → signal `merged_by_human`, WSA-E4-T3).
- `POST /internal/status-comment` — outbound, sob identidade GitHub App.
- `GET /health`.

**Fase 2 (WSA-E4-T3 + WSA-E1-T5):** o webhook de `pull_request` merged dispara
`merged_by_human`; PR fechado sem merge não dispara nada. A resolução de tenant
usa `installation.id` via `tenant_platform_bindings` (fallback documentado para
`DSE_TENANT_ID`). Ver [`../ingest-gateway/README.md`](../ingest-gateway/README.md#fase-2--o-que-ws-a-adicionou).

## Testes

```bash
cd /Users/saraiva/Documents/DSE/fase1/services/adapter-github
pytest -q
```

Resultado desta sessão: **24 passed** (19 Fase 1 + 5 Fase 2: merge webhook +
tenant binding, `tests/test_merge_and_tenant.py`). Requer Postgres real
(`localhost:5432`, migração `0002_wsa.sql` aplicada) — sem mocks de DB.
GitHub em si é 100% fixture (`FakeGithubClient`) nos testes de outbound; a
autenticação GitHub App real (`adapter_github/auth.py`) não é exercitada
em teste automatizado nesta sessão por não haver App/chave privada real —
a lógica (JWT RS256 + troca por installation token) segue o fluxo oficial
da API do GitHub e está pronta para uso assim que as credenciais reais
existirem (ver `GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY`/
`GITHUB_APP_INSTALLATION_ID`).
