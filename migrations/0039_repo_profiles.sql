-- Fintex DSE — what each repository IS, so a request can be routed to it.
--
-- Keyed (tenant_id, repo) and deliberately NOT a set of columns on
-- repo_bindings: that table has one row per BINDING (a channel, a Jira
-- component, a project), so the same repository appears in many rows and a
-- role written on one of them would disagree with the others.
--
-- `description` is the field that does the work. It is one human sentence, and
-- it is what the router reads — a routing decision made from a sentence a human
-- wrote about their own repository is auditable and correctable; one made from
-- a keyword list nobody maintains is neither.
--
-- Additive and idempotent, including the trigger (0023 used a bare CREATE
-- TRIGGER after CREATE TABLE IF NOT EXISTS and is therefore not re-runnable).
CREATE TABLE IF NOT EXISTS repo_profiles (
    tenant_id   TEXT NOT NULL,
    repo        TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT '',
    language    TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, repo)
);

DROP TRIGGER IF EXISTS set_updated_at_repo_profiles ON repo_profiles;
CREATE TRIGGER set_updated_at_repo_profiles
    BEFORE UPDATE ON repo_profiles
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT SELECT, INSERT, UPDATE, DELETE ON repo_profiles TO dse_app;

INSERT INTO repo_profiles (tenant_id, repo, role, language, description) VALUES
  ('fintex-poc', 'andresaraivafintex/bmo-fee-calculator-fe-dse', 'frontend',
   'TypeScript/Angular',
   'Angular UI for the fee calculator: screens, forms, selectors, tables, styling and the fee report view. Everything a user sees or clicks lives here.'),
  ('fintex-poc', 'andresaraivafintex/bmo-fee-calculator-be-dse', 'backend',
   'Java/Spring Boot',
   'Spring Boot REST API for the fee calculator: fee computation rules, currency and rate handling, persistence, and the endpoints the frontend calls.')
ON CONFLICT (tenant_id, repo) DO NOTHING;
