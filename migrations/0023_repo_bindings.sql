-- Fintex DSE — Relatório 07 C2 (Fase B) — resolução de repositório para
-- tarefas que NÃO nascem num repo (Slack/Jira). Cascata determinística (P1):
--   1. override explícito (repo= no texto / campo Jira)  -> no adapter
--   2. binding do "unidade natural de trabalho" -> repo  -> ESTA tabela
--   3. default single-repo do tenant                     -> ESTA tabela
--   4. perguntar (clarificação)                          -> workflow
-- O repo errado nunca acontece em silêncio: sem resolução, o DSE pergunta.
--
-- binding_type ordena a precedência (mais específico -> menos):
--   channel   (Slack #canal)        \
--   component (Jira Component)        > específicos
--   project   (Jira Project)        /
--   workspace (Slack team / Jira site) = fallback amplo
-- Migração aditiva e idempotente.

CREATE TABLE IF NOT EXISTS repo_bindings (
    tenant_id     TEXT NOT NULL,
    platform      TEXT NOT NULL CHECK (platform IN ('slack', 'jira', 'github')),
    binding_type  TEXT NOT NULL CHECK (binding_type IN ('channel', 'component', 'project', 'workspace')),
    binding_value TEXT NOT NULL,             -- ex.: 'C123' (canal), 'Payments' (component), 'FINX' (project)
    repo          TEXT NOT NULL,             -- ex.: 'andre2654/fintex-wallet'
    base_branch   TEXT NOT NULL DEFAULT 'main',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, platform, binding_type, binding_value)
);
CREATE INDEX IF NOT EXISTS idx_repo_bindings_tenant ON repo_bindings (tenant_id);

-- default single-repo por tenant: quando o tenant tem exatamente um repo,
-- toda tarefa sem outro sinal cai nele (remove atrito p/ cliente single-repo).
-- binding_value fixo '*' representa o default.
-- (nenhuma linha inserida aqui — populado no onboarding / console.)

CREATE TRIGGER set_updated_at_repo_bindings
    BEFORE UPDATE ON repo_bindings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT SELECT, INSERT, UPDATE, DELETE ON repo_bindings TO dse_app;
