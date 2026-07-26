-- WS-C — Phase 2 (WSC-E4 skill registry bootstrap + WSC-E5 retrieval/index).
-- Owner: WS-C. Additive — do not edit 0001_foundation.sql nor 0004_wsc.sql.
--
-- `skill_registry` (WSC-E4-T1): catalog of curated skills, TENANT-SCOPED, read
--   by the Planner session (WSC-E3-T3) to hydrate context. Phase 2 has ONLY the
--   registry + the read path — there is NO promotion pipeline (that is Phase 4);
--   rows are seeded by a human (`created_by` = human principal). The approval
--   gate is the `status` field ('approved' is the only one the Planner reads).
--
-- `retrieval_documents` (WSC-E5, ADR-24): retrieval index (repo map + lexical
--   search + self-hosted embeddings). STRICT PER-TENANT ISOLATION — `tenant_id`
--   is NOT NULL on every row and every RetrievalService query filters by it
--   (see sandbox_runtime/retrieval.py and the WS-F isolation suite). The indexed
--   content is UNTRUSTED INPUT for the Planner (flagged as untrusted in the
--   context bundle — never interpreted as an instruction).
--   `embedding` stores the serialized sparse TF-IDF vector (JSONB term->weight)
--   — self-hosted, no GPU (see README: swapping it for sentence-transformers in
--   production is additive, same interface).

-- ---------------------------------------------------------------------------
-- skill_registry
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS skill_registry (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    skill_key      TEXT NOT NULL,          -- stable identifier per tenant (e.g.: 'pci-dss-logging')
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,          -- skill content (human-curated guidance)
    category       TEXT NOT NULL DEFAULT 'general',
    applies_to     JSONB NOT NULL DEFAULT '[]'::jsonb,  -- task_classes/globs the skill applies to
    status         TEXT NOT NULL DEFAULT 'approved'
                     CHECK (status IN ('approved', 'draft', 'retired')),
    created_by     TEXT NOT NULL,          -- HUMAN principal who curated it (P8) — never 'system:*'
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
    tenant_id      TEXT NOT NULL,          -- ISOLATION: every query filters by this
    repo           TEXT NOT NULL,
    path           TEXT NOT NULL,          -- logical path of the doc (file, ticket:ID, repomap)
    kind           TEXT NOT NULL DEFAULT 'file'
                     CHECK (kind IN ('file', 'repo_map', 'ticket', 'doc')),
    content        TEXT NOT NULL,          -- UNTRUSTED content (Planner input)
    content_sha    TEXT NOT NULL,          -- sha256 of the content (reindex idempotency)
    symbols        JSONB NOT NULL DEFAULT '[]'::jsonb,  -- extracted symbols (repo map)
    embedding      JSONB NOT NULL DEFAULT '{}'::jsonb,  -- sparse TF-IDF vector term->weight
    indexed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, repo, path)
);

-- Composite index leading with tenant_id: the planner never does a cross-tenant scan.
CREATE INDEX IF NOT EXISTS idx_retrieval_documents_tenant_repo
    ON retrieval_documents (tenant_id, repo);

GRANT SELECT, INSERT, UPDATE, DELETE ON skill_registry TO dse_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON retrieval_documents TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

-- ---------------------------------------------------------------------------
-- Seed of human-curated skills (bootstrap — WSC-E4-T1). Tenant 'dev' + a second
-- tenant to exercise isolation in the test suite. Idempotent.
-- created_by is a human principal (dse_identity format), never system:*.
-- ---------------------------------------------------------------------------
INSERT INTO skill_registry (tenant_id, skill_key, title, body, category, applies_to, status, created_by) VALUES
  ('dev', 'pci-dss-logging',
   'Never log a PAN or CVV in the clear',
   'When touching code that handles card data: never log a full PAN (mask it to first 6 + last 4), '
   'never log a CVV/CVC under any circumstance, and never persist a CVV. Use the tokenizer in the '
   'payments module. Reference: PCI-DSS 3.4/3.2.',
   'security', '["payments", "default"]'::jsonb, 'approved', 'principal:human:curator-ana'),
  ('dev', 'migrations-reversible',
   'Postgres migrations must be additive and reversible',
   'Every migration must be additive (never DROP COLUMN/TABLE in an online release); use '
   'expand/contract across two releases. Create indexes with CONCURRENTLY, outside a transaction. '
   'Never backfill synchronously in a migration — use a batch job.',
   'database', '["database", "default"]'::jsonb, 'approved', 'principal:human:curator-bruno'),
  ('dev', 'idempotent-webhooks',
   'Webhook handlers must be idempotent',
   'Deduplicate on a persisted event_id; use SELECT ... FOR UPDATE SKIP LOCKED in the dispatcher; '
   'never rely on delivery order. Return 2xx only after the outbox row is persisted.',
   'reliability', '["ingest", "default"]'::jsonb, 'approved', 'principal:human:curator-ana'),
  ('dev', 'draft-not-ready',
   'Draft skill (the Planner must NOT read this)',
   'This skill is a draft. It exists so the suite can prove the Planner reads only status=approved.',
   'general', '["default"]'::jsonb, 'draft', 'principal:human:curator-bruno'),
  ('acme-bank', 'acme-naming',
   'acme-bank naming convention',
   'Internal acme-bank modules use the acme_ prefix; never expose internal IDs in public API '
   'responses. This skill belongs to tenant acme-bank and must NOT leak into tenant dev.',
   'style', '["default"]'::jsonb, 'approved', 'principal:human:curator-acme')
ON CONFLICT (tenant_id, skill_key) DO NOTHING;

INSERT INTO schema_migrations (filename) VALUES ('0010_wsc2.sql')
ON CONFLICT DO NOTHING;
