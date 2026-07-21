-- Fintex DSE — Fase 4 — WSC-E4-T2/T3: multi-versão + proveniência para a
-- esteira de promoção de skill. Dono: WS-C. Migração ADITIVA e idempotente.
--
-- Por que existe (o "faltar algo" do gate da Fase 4): a 0010 criou
-- skill_registry com UNIQUE (tenant_id, skill_key) — UMA linha por skill. A
-- 0019 adicionou `version` e ampliou o CHECK de status, mas o comentário dela
-- promete "rollback por mudança de ponteiro SEM PERDER A VERSÃO ANTERIOR"
-- (failure mode 13) — isso exige que VÁRIAS versões da mesma skill coexistam
-- como linhas distintas. Logo, a unicidade precisa passar de (tenant, key)
-- para (tenant, key, version). Além disso, o candidate materializado (T2)
-- precisa de proveniência (de qual pattern/episódios veio) — colunas novas.

-- 1) Multi-versão: troca a unicidade de (tenant,key) para (tenant,key,version).
--    O nome default da 0010 é skill_registry_tenant_id_skill_key_key.
ALTER TABLE skill_registry DROP CONSTRAINT IF EXISTS skill_registry_tenant_id_skill_key_key;
ALTER TABLE skill_registry DROP CONSTRAINT IF EXISTS skill_registry_tenant_key_version_key;
ALTER TABLE skill_registry ADD CONSTRAINT skill_registry_tenant_key_version_key
    UNIQUE (tenant_id, skill_key, version);

-- 2) Invariante ESTRUTURAL do que o Planner enxerga (P8 evidência-por-construção):
--    NO MÁXIMO uma versão SERVIDA por (tenant, skill_key). O Planner lê
--    status IN ('approved','active'); este índice único parcial torna
--    impossível existirem duas versões servidas da mesma skill ao mesmo tempo
--    — a transição para 'active' TEM de rebaixar a versão servida anterior na
--    mesma transação, ou o INSERT/UPDATE falha. Rollback idem.
DROP INDEX IF EXISTS uq_skill_registry_one_served;
CREATE UNIQUE INDEX uq_skill_registry_one_served
    ON skill_registry (tenant_id, skill_key)
    WHERE status IN ('approved', 'active');

-- 3) Proveniência do candidate materializado (WSC-E4-T2). De qual pattern_key
--    (e quais episódios/occurrences) a skill nasceu — determinístico, auditável.
ALTER TABLE skill_registry ADD COLUMN IF NOT EXISTS pattern_key TEXT;
ALTER TABLE skill_registry ADD COLUMN IF NOT EXISTS provenance JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Índice para achar rápido a próxima versão / a versão servida de uma skill.
CREATE INDEX IF NOT EXISTS idx_skill_registry_tenant_key_version
    ON skill_registry (tenant_id, skill_key, version);

GRANT SELECT, INSERT, UPDATE ON skill_registry TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0020_wsc4.sql') ON CONFLICT DO NOTHING;
