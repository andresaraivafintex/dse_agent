-- Fintex DSE — Teams adapter: ACTIVATION DDL (NOT applied in this session).
--
-- This file is NOT a numbered migration and is NOT applied by
-- scripts/migrate.py. It documents the FOUNDATION changes required to activate
-- the Teams adapter (business/roadmap decision, Phase 4+). Apply it as part of
-- an activation migration, together with the code step:
--
--   CODE (packages/contracts/dse_contracts/conversation_event.py):
--     class Platform(str, Enum):
--         slack = "slack"
--         github = "github"
--         jira = "jira"
--         teams = "teams"          # <-- 1 additive line
--
-- Why it is not applied now: WS-A (Phase 4) does not edit packages/* nor
-- migrations 0001-0019 (coexistence with 4 agents in parallel). The CHECKs below
-- live in the foundation (migration 0001) and mutating them takes locks on hot
-- tables (work_items/identity_links) used by everyone — so activation is
-- deliberate, not a side effect of this delivery.
--
-- All changes are ADDITIVE (they only widen the allowed set). Idempotent.

BEGIN;

-- 1. work_items.source accepts 'teams'
ALTER TABLE work_items DROP CONSTRAINT IF EXISTS work_items_source_check;
ALTER TABLE work_items ADD CONSTRAINT work_items_source_check
    CHECK (source IN ('slack', 'github', 'jira', 'teams'));

-- 2. identity_links.platform accepts 'teams' (for resolve_principal('teams', ...))
ALTER TABLE identity_links DROP CONSTRAINT IF EXISTS identity_links_platform_check;
ALTER TABLE identity_links ADD CONSTRAINT identity_links_platform_check
    CHECK (platform IN ('slack', 'github', 'jira', 'teams'));

COMMIT;

-- After applying this + the enum line: /health starts reporting
-- {"activated": true} and the /teams/messages endpoint moves from the 501
-- teams_not_activated to the full pipeline (correlate/admit) with no other
-- change to the service.
