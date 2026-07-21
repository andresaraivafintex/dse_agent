-- Fintex DSE — Fase 4 — Pipeline de promoção de skill (WSC-E4-T3).
-- Dono: WS-C. Gate de entrada da Fase 4 (adendo 03 §4): a migração 0010 criou
-- skill_registry.status com CHECK (status IN ('approved','draft','retired')).
-- A esteira candidate -> eval -> approved -> canary -> active (+ rolled_back)
-- exige estados novos. Migração aditiva e idempotente.

-- 1) Amplia o CHECK de status. Postgres não tem "ALTER CHECK"; dropa e recria
--    a constraint pelo nome. O nome default gerado pelo Postgres para o CHECK
--    inline da 0010 é `skill_registry_status_check`.
ALTER TABLE skill_registry DROP CONSTRAINT IF EXISTS skill_registry_status_check;
ALTER TABLE skill_registry ADD CONSTRAINT skill_registry_status_check
    CHECK (status IN ('draft', 'candidate', 'approved', 'canary', 'active', 'rolled_back', 'retired'));

-- 2) Versionamento + proveniência da promoção. `version` permite rollback por
--    mudança de ponteiro (failure mode 13) sem perder a versão anterior.
ALTER TABLE skill_registry ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

-- 3) Episódios de skill-learning (WSC-E4-T2) — as três "sources at launch"
--    (§10.17): clarificação recorrente (WS-B), CI-repair (WS-E), review
--    feedback aceito (WS-E). Proveniência completa, tenant-scoped. NENHUMA
--    skill é criada/ativada a partir daqui — é só o insumo governável.
CREATE TABLE IF NOT EXISTS skill_episode (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    source        TEXT NOT NULL CHECK (source IN ('clarification', 'ci_repair', 'review_feedback')),
    work_item_id  TEXT,
    pattern_key   TEXT NOT NULL,          -- agrupa ocorrências do mesmo padrão
    occurrence_n  INTEGER NOT NULL DEFAULT 1,
    provenance    JSONB NOT NULL DEFAULT '{}'::jsonb,  -- PR, reviewer, diff, etc.
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_skill_episode_tenant_pattern
    ON skill_episode (tenant_id, pattern_key);

-- 4) Trilha de eval de cada candidate (WSC-E4-T3) — replay contra o eval set.
CREATE TABLE IF NOT EXISTS skill_eval (
    id                 BIGSERIAL PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    skill_key          TEXT NOT NULL,
    candidate_version  INTEGER NOT NULL,
    passed             BOOLEAN NOT NULL,
    score              DOUBLE PRECISION NOT NULL DEFAULT 0,
    positive_hits      INTEGER NOT NULL DEFAULT 0,
    negative_regressions INTEGER NOT NULL DEFAULT 0,
    detail             TEXT NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_skill_eval_key
    ON skill_eval (tenant_id, skill_key, candidate_version);

GRANT SELECT, INSERT, UPDATE ON skill_registry TO dse_app;
GRANT SELECT, INSERT ON skill_episode, skill_eval TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0019_wsc4.sql') ON CONFLICT DO NOTHING;
