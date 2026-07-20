-- WS-C — Fase 2 (WSC-E4 skill registry bootstrap + WSC-E5 retrieval/index).
-- Dono: WS-C. Aditivo — não editar 0001_foundation.sql nem 0004_wsc.sql.
--
-- `skill_registry` (WSC-E4-T1): catálogo de skills curadas, TENANT-SCOPED, lido
--   pela sessão Planner (WSC-E3-T3) para hidratar contexto. Fase 2 tem SÓ o
--   registry + a leitura — NÃO há pipeline de promoção (isso é Fase 4); as
--   linhas são semeadas por humano (`created_by` = principal humano). O gate
--   de aprovação é o campo `status` ('approved' é o único que o Planner lê).
--
-- `retrieval_documents` (WSC-E5, ADR-24): índice de recuperação (repo map +
--   busca lexical + embeddings self-hosted). ISOLAMENTO POR TENANT RIGOROSO —
--   `tenant_id` é NOT NULL em toda linha e toda query do RetrievalService
--   filtra por ele (ver sandbox_runtime/retrieval.py e a suíte de isolamento
--   do WS-F). O conteúdo indexado é INPUT NÃO CONFIÁVEL do Planner (marcado
--   como untrusted no bundle de contexto — nunca interpretado como instrução).
--   `embedding` guarda o vetor TF-IDF esparso serializado (JSONB term->weight)
--   — self-hosted, sem GPU (ver README: troca por sentence-transformers em
--   produção é aditiva, mesma interface).

-- ---------------------------------------------------------------------------
-- skill_registry
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS skill_registry (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    skill_key      TEXT NOT NULL,          -- identificador estável por tenant (ex.: 'pci-dss-logging')
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,          -- conteúdo da skill (guidance curada por humano)
    category       TEXT NOT NULL DEFAULT 'general',
    applies_to     JSONB NOT NULL DEFAULT '[]'::jsonb,  -- task_classes/globs a que a skill se aplica
    status         TEXT NOT NULL DEFAULT 'approved'
                     CHECK (status IN ('approved', 'draft', 'retired')),
    created_by     TEXT NOT NULL,          -- principal HUMANO que curou (P8) — nunca 'system:*'
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, skill_key)
);

CREATE INDEX IF NOT EXISTS idx_skill_registry_tenant_approved
    ON skill_registry (tenant_id) WHERE status = 'approved';

DROP TRIGGER IF EXISTS trg_skill_registry_updated_at ON skill_registry;
CREATE TRIGGER trg_skill_registry_updated_at
    BEFORE UPDATE ON skill_registry
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- retrieval_documents
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS retrieval_documents (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      TEXT NOT NULL,          -- ISOLAMENTO: toda query filtra por isto
    repo           TEXT NOT NULL,
    path           TEXT NOT NULL,          -- caminho lógico do doc (arquivo, ticket:ID, repomap)
    kind           TEXT NOT NULL DEFAULT 'file'
                     CHECK (kind IN ('file', 'repo_map', 'ticket', 'doc')),
    content        TEXT NOT NULL,          -- conteúdo NÃO CONFIÁVEL (input do Planner)
    content_sha    TEXT NOT NULL,          -- sha256 do content (idempotência de reindex)
    symbols        JSONB NOT NULL DEFAULT '[]'::jsonb,  -- símbolos extraídos (repo map)
    embedding      JSONB NOT NULL DEFAULT '{}'::jsonb,  -- vetor TF-IDF esparso term->weight
    indexed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, repo, path)
);

-- Índice composto começando por tenant_id: o planner nunca faz scan cross-tenant.
CREATE INDEX IF NOT EXISTS idx_retrieval_documents_tenant_repo
    ON retrieval_documents (tenant_id, repo);

GRANT SELECT, INSERT, UPDATE, DELETE ON skill_registry TO dse_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON retrieval_documents TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

-- ---------------------------------------------------------------------------
-- Seed de skills curadas humanas (bootstrap — WSC-E4-T1). Tenant 'dev' + um
-- segundo tenant para exercitar isolamento na suíte de testes. Idempotente.
-- created_by é um principal humano (formato do dse_identity), nunca system:*.
-- ---------------------------------------------------------------------------
INSERT INTO skill_registry (tenant_id, skill_key, title, body, category, applies_to, status, created_by) VALUES
  ('dev', 'pci-dss-logging',
   'Nunca logar PAN/CVV em claro',
   'Ao mexer em código que toca dados de cartão: nunca logar PAN completo (mascare para 6+4), '
   'nunca logar CVV/CVC em nenhuma circunstância, nunca persistir CVV. Use o tokenizador do '
   'módulo payments. Referência: PCI-DSS 3.4/3.2.',
   'security', '["payments", "default"]'::jsonb, 'approved', 'principal:human:curator-ana'),
  ('dev', 'migrations-reversible',
   'Migrações Postgres precisam ser reversíveis e aditivas',
   'Toda migração deve ser aditiva (nunca DROP COLUMN/TABLE numa release online); use '
   'expand/contract em duas releases. Adicione índices com CONCURRENTLY fora de transação. '
   'Nunca faça backfill síncrono numa migração — use job em batch.',
   'database', '["database", "default"]'::jsonb, 'approved', 'principal:human:curator-bruno'),
  ('dev', 'idempotent-webhooks',
   'Handlers de webhook devem ser idempotentes',
   'Deduplique por event_id persistido; use SELECT ... FOR UPDATE SKIP LOCKED para o dispatcher; '
   'nunca confie na ordem de entrega. Retorne 2xx só após persistir o outbox.',
   'reliability', '["ingest", "default"]'::jsonb, 'approved', 'principal:human:curator-ana'),
  ('dev', 'draft-not-ready',
   'Skill em rascunho (NÃO deve ser lida pelo Planner)',
   'Esta skill está em draft e serve ao teste de que o Planner só lê status=approved.',
   'general', '["default"]'::jsonb, 'draft', 'principal:human:curator-bruno'),
  ('acme-bank', 'acme-naming',
   'Convenção de nomes do acme-bank',
   'Módulos internos do acme-bank usam prefixo acme_; nunca exponha IDs internos em respostas '
   'de API públicas. Esta skill pertence ao tenant acme-bank e NÃO deve vazar para o tenant dev.',
   'style', '["default"]'::jsonb, 'approved', 'principal:human:curator-acme')
ON CONFLICT (tenant_id, skill_key) DO NOTHING;

INSERT INTO schema_migrations (filename) VALUES ('0010_wsc2.sql')
ON CONFLICT DO NOTHING;
