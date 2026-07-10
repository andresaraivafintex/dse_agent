# Fintex DSE — Fase 1 ("Core loop")

Implementação da Fase 1 do plano de desenvolvimento (`../plano-desenvolvimento/00-PLANO-MESTRE.md`
§3): o ciclo completo Slack/GitHub → clarificação → Coder único em sandbox → L1 → PR
determinístico → review humano → merge humano, durável (Temporal) e auditável (audit ledger
append-only).

**Antes de tocar em código, leia [`CONVENTIONS.md`](./CONVENTIONS.md)** — define stack, dono de
cada diretório, numeração de migrações e as portas/contratos compartilhados.

## Status

Ver `docs/PHASE1-STATUS.md` (gerado após a integração) para o mapa completo entre os critérios
de saída da Fase 1 (Seção 16 da proposta) e o que está implementado, mockado, ou pendente de
credenciais/infra reais (conta AWS/Bedrock, Apps Slack/GitHub registrados, cluster K8s do
cliente) que este ambiente de desenvolvimento não pode provisionar sozinho.

## Como rodar

```
make up        # sobe Postgres, Temporal (+UI), Redis, Vault-dev + serviços de cada workstream
make migrate   # aplica migrations/*.sql em ordem
make install   # pip install -e em todos os pacotes/serviços Python
make test      # roda a suíte de testes (requer make up + make migrate antes)
make down
```

## Estrutura

```
packages/contracts/    ConversationEvent, WorkItem, PlanArtifact, gateway contract, mutable comment
packages/dse_audit/    cliente do audit ledger append-only
packages/dse_identity/ resolução platform_user_id -> principal (fundações do identity map)
services/adapter-slack/    services/adapter-github/    services/ingest-gateway/       (WS-A)
services/orchestrator/                                                               (WS-B)
services/sandbox-runtime/  services/egress-proxy/                                    (WS-C)
services/model-gateway/                                                              (WS-D)
services/validation/                                                                 (WS-E)
services/platform/                                                                   (WS-F)
migrations/            schema SQL numerado (fundação + 1 arquivo reservado por workstream)
infra/                 skeletons de Helm/Terraform para o deployment real no VPC do cliente
```
