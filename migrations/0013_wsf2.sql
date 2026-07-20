-- Fintex DSE — Fase 2 ("Judgment & queue") — WS-F (segurança, compliance,
-- plataforma e operações).
-- Dono: WS-F. Arquivo reservado (ver CONVENTIONS.md, tabela de migrações da
-- Fase 2) — não editar fora do WS-F. Aditivo sobre 0001–0007 (não altera
-- nenhuma tabela da fundação nem de outro workstream).
--
-- Tabelas:
--   1. dse_access_bundle       — WSF-E3-T2: bundle de acesso por (tenant, canal)
--   2. dse_console_identity    — WSF-E3-T3: usuários do console admin (SSO/ADR-22),
--                                offboarding remove da resolução de approver/steering
--   3. dse_kill_switch_global  — WSF-E6-T2: kill switch de escopo GLOBAL (o 4º escopo;
--                                tenant=tenant_config, canal=channel_kill_switches[WS-A],
--                                task=signal Temporal)
--   4. dse_work_item_quarantine — WSF-E6-T2: quarentena durável de um work item
--                                (par do signal de pause enviado ao workflow)

-- ---------------------------------------------------------------------------
-- 1. dse_access_bundle (§10.18) — o "bundle" de acesso administrável por
-- tenant/canal. Um bundle com channel = NULL é o default do tenant; um bundle
-- com channel preenchido sobrepõe o default para aquele canal específico
-- (resolução: canal-específico primeiro, senão o default do tenant).
--
-- Enforcement (dse_platform.access_bundles) é consultado nos pontos de decisão:
--   - repos permitidos            -> intake/ingest-gateway (WS-A) e sandbox (WS-C)
--   - modes                       -> gate de plano / seleção de fluxo (WS-B)
--   - blocked_actions             -> ex.: direct_merge_to_protected_branch (WS-E/WS-B)
--   - designated_approvers        -> FALLBACK da cascata CODEOWNERS do gate de
--                                    plano do WS-B (WSB-E3-T2). Cascata vazia
--                                    BLOQUEIA (nunca auto-aprova — P1/P3).
--   - learning_scope              -> escopo de promoção de skill (WS-C, bootstrap)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dse_access_bundle (
    id                    BIGSERIAL PRIMARY KEY,
    tenant_id             TEXT NOT NULL,
    channel               TEXT,                  -- NULL = default do tenant; senão canal específico
    allowed_repos         JSONB NOT NULL DEFAULT '[]'::jsonb,   -- lista de "owner/repo"; [] = nenhum
    modes                 JSONB NOT NULL DEFAULT '[]'::jsonb,   -- subset de scope|ask|implement_low_risk|security_review
    budgets               JSONB NOT NULL DEFAULT '{}'::jsonb,   -- {"monthly_usd": .., "per_task_usd": ..}
    blocked_actions       JSONB NOT NULL DEFAULT '[]'::jsonb,   -- ex.: ["direct_merge_to_protected_branch"]
    designated_approvers  JSONB NOT NULL DEFAULT '[]'::jsonb,   -- lista de principal_id (fallback da cascata)
    learning_scope        TEXT NOT NULL DEFAULT 'none'
                            CHECK (learning_scope IN ('none', 'tenant', 'global')),
    enabled               BOOLEAN NOT NULL DEFAULT true,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Um único bundle por (tenant, channel). O default do tenant (channel NULL)
-- precisa de índice único parcial próprio porque NULL não é comparável em UNIQUE.
CREATE UNIQUE INDEX IF NOT EXISTS uq_access_bundle_tenant_channel
    ON dse_access_bundle (tenant_id, channel) WHERE channel IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_access_bundle_tenant_default
    ON dse_access_bundle (tenant_id) WHERE channel IS NULL;
CREATE INDEX IF NOT EXISTS idx_access_bundle_tenant ON dse_access_bundle (tenant_id);

DROP TRIGGER IF EXISTS trg_access_bundle_updated_at ON dse_access_bundle;
CREATE TRIGGER trg_access_bundle_updated_at
    BEFORE UPDATE ON dse_access_bundle
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- 2. dse_console_identity (WSF-E3-T3 / ADR-22) — usuário do console admin
-- autenticado via SSO (OIDC/SAML). `sso_subject` é o `sub` do IdP (account
-- matching por subject estável, não por email — ver infra/ADR-22-identity.md).
-- `principal_id` liga ao identity map da fundação (dse_identity). `active`
-- false = offboardado: removido da resolução de approver/steering E do login.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dse_console_identity (
    principal_id     TEXT PRIMARY KEY REFERENCES principals (id),
    sso_subject      TEXT NOT NULL UNIQUE,       -- `sub` claim do IdP (estável entre logins)
    email            TEXT,
    display_name     TEXT,
    tenant_id        TEXT,                        -- tenant "home" (NULL = operador multi-tenant/plataforma)
    roles            JSONB NOT NULL DEFAULT '[]'::jsonb,   -- ex.: ["operator","approver"]
    is_contractor    BOOLEAN NOT NULL DEFAULT false,       -- ADR-22: contractors têm expiração
    active           BOOLEAN NOT NULL DEFAULT true,        -- false = offboardado
    expires_at       TIMESTAMPTZ,                 -- contractors: acesso expira (NULL = sem expiração)
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    deactivated_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_console_identity_tenant ON dse_console_identity (tenant_id);
CREATE INDEX IF NOT EXISTS idx_console_identity_active ON dse_console_identity (active) WHERE active;

DROP TRIGGER IF EXISTS trg_console_identity_updated_at ON dse_console_identity;
CREATE TRIGGER trg_console_identity_updated_at
    BEFORE UPDATE ON dse_console_identity
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- 3. dse_kill_switch_global — o 4º escopo de kill switch (o mais amplo).
-- Linha única (id = 'global'). Ligado = TODA admissão de trabalho, em todos os
-- tenants, é bloqueada. Consultado pelo ingest-gateway (WS-A) e pelo
-- model-gateway (WS-D) antes do tenant/canal. Mudança sempre auditada (P8).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dse_kill_switch_global (
    id           TEXT PRIMARY KEY DEFAULT 'global' CHECK (id = 'global'),
    enabled      BOOLEAN NOT NULL DEFAULT false,
    reason       TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_kill_switch_global_updated_at ON dse_kill_switch_global;
CREATE TRIGGER trg_kill_switch_global_updated_at
    BEFORE UPDATE ON dse_kill_switch_global
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO dse_kill_switch_global (id, enabled) VALUES ('global', false)
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 4. dse_work_item_quarantine — quarentena durável de um work item (par do
-- signal `pause`/`escalate` enviado ao workflow pelo operador). Fica no banco
-- para que a decisão sobreviva a restart do worker e para que a projeção do
-- queue board mostre "quarentined" mesmo sem query ao workflow. released_at
-- preenchido = liberado.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dse_work_item_quarantine (
    work_item_id   TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    reason         TEXT NOT NULL,
    actor          TEXT NOT NULL,               -- principal do operador (nunca platform_user_id bruto)
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at    TIMESTAMPTZ,
    released_by    TEXT
);

CREATE INDEX IF NOT EXISTS idx_quarantine_tenant ON dse_work_item_quarantine (tenant_id);
CREATE INDEX IF NOT EXISTS idx_quarantine_active
    ON dse_work_item_quarantine (work_item_id) WHERE released_at IS NULL;

GRANT SELECT, INSERT, UPDATE ON dse_access_bundle TO dse_app;
GRANT SELECT, INSERT, UPDATE ON dse_console_identity TO dse_app;
GRANT SELECT, INSERT, UPDATE ON dse_kill_switch_global TO dse_app;
GRANT SELECT, INSERT, UPDATE ON dse_work_item_quarantine TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0013_wsf2.sql')
ON CONFLICT DO NOTHING;
