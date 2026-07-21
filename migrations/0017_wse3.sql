-- Fintex DSE — Fase 3 ("Evidence") — WS-E (services/validation).
-- Migração reservada 0017 para WS-E na Fase 3 (CONVENTIONS.md §Fase 3).
-- Não editar 0001-0013 (fundação/Fases 1-2). Este arquivo só cria tabelas de WS-E.
--
-- Adições:
--   wse_artifacts             — WSE-E5-T12: registro de cada artefato publicado no
--                               Garage (chave S3 prefixada por WorkItem dentro do
--                               bucket por tenant, NFR-03). `quarantined_at` marca a
--                               invalidação de acesso ANTES do TTL quando o work item
--                               entra em quarentena (costura com WS-F,
--                               dse_work_item_quarantine).
--   wse_artifact_access_log   — WSE-E5-T12: 1 linha por RESOLUÇÃO de link de evidência
--                               (presign/abertura), associável ao PR — insumo da
--                               métrica "evidence consumption".
--   wse_previews              — WSE-E4-T10: estado de cada preview environment por PR
--                               (namespace efêmero no cluster k3d, TTL, status).
--   wse_preview_caps          — ADR-26: cap de previews concorrentes por tenant desde
--                               o dia 1 (linha ausente => default do env).
--   wse_ci_reruns             — WSE-E4-T9b: evidência de cada targeted re-run pedido
--                               em fix commit (re-run só dos check-runs falhos).
--   wse_ci_repair_episodes    — WSE-E4-T9b: episódios de skill-learning de padrões
--                               repetidos de CI-repair (tenant-scoped, com
--                               proveniência). NENHUMA skill é criada/ativada aqui —
--                               só o episódio (promoção de skills é Fase 4).
--   wse_evidence_publications — WSE-E5-T14/ADR-26: estado do debounce de refresh de
--                               evidência (última publicação consolidada por WorkItem).

CREATE TABLE IF NOT EXISTS wse_artifacts (
    id              BIGSERIAL PRIMARY KEY,
    work_item_id    TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,
    kind            TEXT NOT NULL,             -- demo_video | playwright_trace | visual_diff | visual_baseline | test_report
    bucket          TEXT NOT NULL,             -- bucket por tenant (NFR-03)
    store_key       TEXT NOT NULL,             -- chave S3 dentro do bucket
    content_type    TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes      BIGINT NOT NULL DEFAULT 0,
    multipart       BOOLEAN NOT NULL DEFAULT FALSE,  -- upload multipart (ADR-18 revisado)
    ttl_seconds     INTEGER NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,      -- expiração POR POLÍTICA do link presigned
    quarantined_at  TIMESTAMPTZ,               -- setado quando movido p/ prefixo de quarentena
    quarantine_key  TEXT,                      -- chave nova sob quarantine/ (acesso negado)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bucket, store_key)
);

CREATE INDEX IF NOT EXISTS idx_wse_artifacts_work_item
    ON wse_artifacts (work_item_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wse_artifacts_tenant
    ON wse_artifacts (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS wse_artifact_access_log (
    id            BIGSERIAL PRIMARY KEY,
    artifact_id   BIGINT REFERENCES wse_artifacts (id),
    work_item_id  TEXT NOT NULL,
    tenant_id     TEXT NOT NULL,
    pr_number     INTEGER,                     -- associável ao PR (métrica evidence consumption)
    accessor      TEXT NOT NULL,               -- principal resolvido ou system:<component>
    via           TEXT NOT NULL DEFAULT 'presign',  -- presign | tracking_comment | api
    accessed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wse_artifact_access_work_item
    ON wse_artifact_access_log (work_item_id, accessed_at DESC);

CREATE TABLE IF NOT EXISTS wse_previews (
    work_item_id  TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    pr_number     INTEGER NOT NULL,
    repo          TEXT NOT NULL,
    status        TEXT NOT NULL,               -- created | skipped_backend_only | degraded | reaped
    namespace     TEXT,                        -- preview-<work_item_id> quando created
    url           TEXT,
    detail        TEXT NOT NULL DEFAULT '',
    ttl_seconds   INTEGER NOT NULL DEFAULT 3600,
    expires_at    TIMESTAMPTZ,                 -- TTL do namespace efêmero
    reaped_at     TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wse_previews_tenant_active
    ON wse_previews (tenant_id) WHERE status = 'created' AND reaped_at IS NULL;

CREATE TABLE IF NOT EXISTS wse_preview_caps (
    tenant_id       TEXT PRIMARY KEY,
    max_concurrent  INTEGER NOT NULL CHECK (max_concurrent >= 0)
);

CREATE TABLE IF NOT EXISTS wse_ci_reruns (
    id             BIGSERIAL PRIMARY KEY,
    work_item_id   TEXT NOT NULL,
    tenant_id      TEXT NOT NULL,
    pr_number      INTEGER NOT NULL,
    fix_commit_sha TEXT NOT NULL,
    check_run_ids  JSONB NOT NULL DEFAULT '[]'::jsonb,  -- ids re-rodados (só os falhos)
    check_names    JSONB NOT NULL DEFAULT '[]'::jsonb,
    requested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wse_ci_reruns_work_item
    ON wse_ci_reruns (work_item_id, requested_at DESC);

CREATE TABLE IF NOT EXISTS wse_ci_repair_episodes (
    id                 BIGSERIAL PRIMARY KEY,
    tenant_id          TEXT NOT NULL,           -- tenant-scoped (nunca cruza tenant)
    work_item_id       TEXT NOT NULL,
    check_name         TEXT NOT NULL,
    failure_signature  TEXT NOT NULL,           -- assinatura determinística do padrão de falha
    fix_commit_sha     TEXT NOT NULL,
    occurrence_n       INTEGER NOT NULL DEFAULT 1, -- nº de vezes que (tenant, assinatura) já ocorreu
    provenance         JSONB NOT NULL DEFAULT '{}'::jsonb, -- pr, repo, shas, timestamps
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wse_ci_repair_episodes_pattern
    ON wse_ci_repair_episodes (tenant_id, failure_signature, created_at DESC);

CREATE TABLE IF NOT EXISTS wse_evidence_publications (
    work_item_id     TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    last_commit_sha  TEXT NOT NULL DEFAULT '',
    fingerprint      TEXT NOT NULL DEFAULT '',  -- hash dos links publicados (evita edit no-op)
    published_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON wse_artifacts TO dse_app;
GRANT SELECT, INSERT ON wse_artifact_access_log TO dse_app;
GRANT SELECT, INSERT, UPDATE ON wse_previews TO dse_app;
GRANT SELECT, INSERT, UPDATE ON wse_preview_caps TO dse_app;
GRANT SELECT, INSERT ON wse_ci_reruns TO dse_app;
GRANT SELECT, INSERT ON wse_ci_repair_episodes TO dse_app;
GRANT SELECT, INSERT, UPDATE ON wse_evidence_publications TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0017_wse3.sql')
ON CONFLICT DO NOTHING;
