-- Fintex DSE — Fase 4 ("Loop hardening & learning") — WS-E (services/validation).
-- Migração reservada 0020 para WS-E na Fase 4 (adendo 03 §4; enunciado do WS-E).
-- Não editar 0001-0019 (fundação/Fases 1-3, e a 0019 é do WS-C). Este arquivo só
-- cria tabela de WS-E. Aditiva e idempotente.
--
-- Adições:
--   wse_base_updates — WSE-E6-T16 (merge-base, nunca rebase durante review):
--                      1 linha de EVIDÊNCIA (P8) por execução de update_base_branch.
--                      Registra a estratégia escolhida (merge_base / rebase_prefirst_review
--                      / noop_no_drift), se houve conflito não-resolvível (escalado a
--                      humano — o agente NUNCA resolve à força) e a asserção de exit da
--                      Fase 4: `orphaned_threads` — DEVE ser 0 no caminho merge-base
--                      (as threads de review ancoradas em commits são preservadas; o
--                      rebase+force-push as órfã — failure mode 11).
--
-- Episódios de skill-learning de REVIEW FEEDBACK (WSE-E6-T18) NÃO ganham tabela
-- nova: usam `skill_episode` (source='review_feedback') da migração 0019 (WS-C) —
-- a fronteira "só episódio, nenhuma skill criada/ativada" é a mesma dos episódios
-- de CI-repair (wse_ci_repair_episodes, Fase 3). WS-C consome os episódios no
-- pipeline de promoção (WSC-E4-T2/T3).

CREATE TABLE IF NOT EXISTS wse_base_updates (
    id                       BIGSERIAL PRIMARY KEY,
    work_item_id             TEXT NOT NULL,
    tenant_id                TEXT NOT NULL,
    repo                     TEXT NOT NULL,
    branch                   TEXT NOT NULL,
    base_branch              TEXT NOT NULL,
    strategy                 TEXT NOT NULL CHECK (strategy IN
                                 ('merge_base', 'rebase_prefirst_review', 'noop_no_drift')),
    conflict                 BOOLEAN NOT NULL DEFAULT FALSE,
    orphaned_threads         INTEGER NOT NULL DEFAULT 0,
    anchored_threads         INTEGER NOT NULL DEFAULT 0,  -- nº de threads de review ancoradas observadas
    first_human_review_done  BOOLEAN NOT NULL DEFAULT TRUE,
    old_tip_sha              TEXT NOT NULL DEFAULT '',
    new_tip_sha              TEXT NOT NULL DEFAULT '',
    detail                   TEXT NOT NULL DEFAULT '',
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wse_base_updates_work_item
    ON wse_base_updates (work_item_id, created_at DESC);

GRANT SELECT, INSERT ON wse_base_updates TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0020_wse4.sql')
ON CONFLICT DO NOTHING;
