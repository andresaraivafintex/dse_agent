-- Fintex DSE — Fase 2 — WS-E (L2 fresh-context loop + modo estrito de PR)
-- Dono: WS-E (services/validation). Migração reservada 0012 para WS-E na Fase 2.
-- Não editar 0001-0007 (fundação). Este arquivo só toca tabelas de WS-E.
--
-- Adições:
--   wse_l2_reviews   — WSE-E2-T4: 1 linha por execução da sessão Reviewer L2
--                       (contexto fresco, só plan+diff — P3). Evidência do
--                       veredito, das objeções e do CUSTO por iteração do loop
--                       de fix-retries (WSE-E2-T5), além do audit_log genérico.
--   wse_fix_loops    — WSE-E2-T5: estado durável do loop bounded L2->Coder por
--                       WorkItem: nº de iterações consumidas e budget debitado.
--                       O workflow do WS-B é dono da orquestração de estados; esta
--                       tabela é a evidência/contador consultado pela lógica de
--                       decisão determinística deste workstream (nenhum LLM decide).
--
-- Alterações aditivas em wse_pr_tracking (criada em 0006_wse.sql) para o MODO
-- ESTRITO (WSE-E3-T8, desbloqueado por PrRef.compare_url no contrato da Fase 2):
--   - pr_number passa a aceitar NULL (branch empurrada + compare link postado,
--     PR ainda NÃO aberto — um humano abre com 1 clique);
--   - compare_url: link de compare postado quando pr_number IS NULL.
-- Mudança aditiva e idempotente: todo caller da Fase 1 continua gravando
-- pr_number preenchido; nada existente quebra.

CREATE TABLE IF NOT EXISTS wse_l2_reviews (
    id            BIGSERIAL PRIMARY KEY,
    work_item_id  TEXT NOT NULL,
    tenant_id     TEXT NOT NULL,
    iteration     INTEGER NOT NULL DEFAULT 0,
    passed        BOOLEAN NOT NULL,
    objections    JSONB NOT NULL DEFAULT '[]'::jsonb,
    cost_usd      NUMERIC(12, 6) NOT NULL DEFAULT 0,
    run_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wse_l2_reviews_work_item
    ON wse_l2_reviews (work_item_id, run_at DESC);

CREATE TABLE IF NOT EXISTS wse_fix_loops (
    work_item_id   TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    iterations     INTEGER NOT NULL DEFAULT 0,   -- retornos ao Coder já consumidos
    spent_usd      NUMERIC(12, 6) NOT NULL DEFAULT 0,
    exhausted      BOOLEAN NOT NULL DEFAULT FALSE, -- true quando escalado a operador
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Modo estrito (WSE-E3-T8): pr_number opcional + compare_url.
ALTER TABLE wse_pr_tracking ALTER COLUMN pr_number DROP NOT NULL;
ALTER TABLE wse_pr_tracking ADD COLUMN IF NOT EXISTS compare_url TEXT;

GRANT SELECT, INSERT ON wse_l2_reviews TO dse_app;
GRANT SELECT, INSERT, UPDATE ON wse_fix_loops TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0012_wse2.sql')
ON CONFLICT DO NOTHING;
