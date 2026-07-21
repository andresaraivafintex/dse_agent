-- Fintex DSE — Fase 3 — WS-F (arquivo reservado, ver CONVENTIONS.md §Fase 3)
-- WSF-E8-T2: retenção por classificação de dados (§12.2).
--
-- Política configurável por tenant/classe vive em `tenant_config.retention`
-- (JSONB) — a tabela é do próprio WS-F (0007_wsf.sql), então o ALTER abaixo
-- não toca schema de outro workstream. Shape do JSONB (validado em
-- dse_platform/retention.py, nunca "schema-less de verdade"):
--
--   {"<data_class>": {"days": <int > 0>}, ...}
--   ex.: {"internal": {"days": 90}, "restricted": {"days": 30}}
--
-- Classe SEM entrada = SEM expurgo (conservador por default: retenção é uma
-- decisão explícita por tenant, nunca um default silencioso que apaga dados).
--
-- audit_log NÃO entra em nenhuma política daqui: é append-only com retenção
-- compliance-grade própria (0001_foundation.sql revoga UPDATE/DELETE até da
-- role de app — a garantia é estrutural). dse_platform/retention.py também
-- recusa qualquer alvo 'audit_log%' em código (defesa em profundidade).

ALTER TABLE tenant_config
    ADD COLUMN IF NOT EXISTS retention JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Índice de apoio ao job de expurgo (varre por idade). Objeto NOVO sobre a
-- tabela da fundação (não edita 0001) — mesma regra dos GRANTs aditivos.
CREATE INDEX IF NOT EXISTS idx_ingest_events_received_at
    ON ingest_events (received_at);

INSERT INTO schema_migrations (filename) VALUES ('0018_wsf3.sql')
ON CONFLICT DO NOTHING;
