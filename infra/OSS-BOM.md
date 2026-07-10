# OSS Bill of Materials — Fintex DSE (Fase 1)

Dono: WS-F. Lista os componentes open-source de infraestrutura usados no
steady-state (imagens de container + bibliotecas Python de maior porte),
suas licenças, e a implicação para um cliente fintech regulado (self-hosted,
sem obrigação de compartilhamento de código proprietário do cliente).

Este documento cobre **infraestrutura e runtime**, não todas as
transitivas de `pip` (essas são auditadas via `pip-licenses`/SBOM de
supply-chain — fora do escopo da Fase 1, ver nota no final).

## Componentes de infraestrutura (imagens de container)

| Componente | Imagem | Licença | Uso no DSE | Obrigações |
|---|---|---|---|---|
| PostgreSQL | `postgres:16-alpine` | PostgreSQL License (permissiva, estilo MIT/BSD) | Control plane (work_items, audit_log, tenant_config, identity map) | Nenhuma — atribuição opcional, sem copyleft |
| Temporal | `temporalio/auto-setup`, `temporalio/ui` | MIT | Orquestração durável do ciclo de vida do WorkItem | Nenhuma — permissiva |
| Redis | `redis:7-alpine` | RSALv2 / SSPLv1 (dual license, Redis 7.x) — **atenção**: a partir do Redis 7.4/8, a Redis Inc. mudou a licença de BSD para RSAL/SSPL | Cache/fila leve (uso interno, não como serviço multi-tenant hospedado) | Uso self-hosted interno (não oferecido como serviço a terceiros) tipicamente cai fora do gatilho do SSPL, mas **recomendação WS-F**: avaliar migração para `valkey` (fork BSD-3-Clause mantido pela Linux Foundation) antes do primeiro cliente de produção, ou confirmar com jurídico que o uso interno não aciona SSPL/RSAL |
| HashiCorp Vault | `hashicorp/vault:1.17` | BUSL 1.1 (Business Source License) desde Vault 1.11+ — **não é OSS puro** | Secrets backend (dev mode nesta Fase 1) | BUSL permite uso em produção não-competitivo (não oferecer Vault como serviço concorrente da HashiCorp) — uso interno do DSE está coberto; documentar para o cliente que licenciamento comercial da HashiCorp pode ser necessário dependendo do volume/suporte desejado. Alternativa 100% OSS (MPL 2.0): OpenBao (fork da Linux Foundation pós-BUSL) — avaliar antes do piloto se BUSL for bloqueio contratual |
| OpenTelemetry Collector | `otel/opentelemetry-collector-contrib` | Apache 2.0 | Coleta de spans/métricas (WSF-E7-T1) | Nenhuma — permissiva |

## Bibliotecas Python principais (runtime, não dev-only)

| Biblioteca | Licença | Uso |
|---|---|---|
| LiteLLM | MIT | Model gateway (WS-D) — proxy unificado para providers de LLM |
| FastAPI | MIT | Todo serviço HTTP (adapters, ingest-gateway, validation, platform) |
| Pydantic v2 | MIT | Validação de dados (contratos `dse_contracts`) |
| Temporal Python SDK | MIT | Workers/workflows (`services/orchestrator`) |
| psycopg2-binary | LGPL 3.0 (com exceção — permite linking dinâmico sem copyleft de aplicação) | Driver Postgres |
| hvac | Apache 2.0 | Cliente Python para Vault (`services/platform/dse_secrets`) |
| requests | Apache 2.0 | HTTP client (fallback do `dse_secrets` sem hvac) |

## Ação recomendada antes do primeiro cliente de produção

1. **Vault (BUSL)**: decisão jurídica explícita — aceitar BUSL para uso
   interno self-hosted, ou migrar para OpenBao (MPL 2.0). Bloqueia
   `WSF-E5-T3` (topologia B) se o cliente exigir OSS puro por contrato.
2. **Redis (RSAL/SSPL)**: mesma decisão — migrar para Valkey é uma troca de
   imagem de baixo esforço (protocolo compatível) se necessário.
3. **SBOM completo de transitivas**: rodar `pip-licenses` (ou `cyclonedx-py`)
   em cada `pyproject.toml` do monorepo e anexar a este documento — não feito
   nesta sessão porque cada workstream ainda está fixando suas próprias
   dependências em paralelo (faria sentido só na fase de consolidação, com
   o dependency tree final congelado).
