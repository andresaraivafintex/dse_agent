-- Fintex DSE — Skills per repository (console ⇄ engine integration).
-- Owner: WS-C. Additive and idempotent.
--
-- The console (dse_console_pane, Skill Registry) is the central skill store:
-- there is no "install" — the operator TICKS, per repo, which skills run. That
-- tick lives here, in `repo_scope`:
--   NULL           -> global skill (legacy/native to fase1; serves any repo)
--   '["*"]'        -> ticked for every repo
--   '["o/r", ...]' -> ticked only for those repos (owner/name)
--   '[]'           -> ticked for no repo (not served to runs)
-- The tenant-scoped read stays in read_approved_skills (P6 fail-closed); the
-- per-repo filter is applied there. The body (SKILL.md) remains in `body` and is
-- materialized into `.claude/skills/<skill_key>/SKILL.md` in the sandbox
-- workspace before the agent's turn (WSC — skill_files.py).

ALTER TABLE skill_registry ADD COLUMN IF NOT EXISTS repo_scope JSONB DEFAULT NULL;

COMMENT ON COLUMN skill_registry.repo_scope IS
    'Ticks por repo do console: NULL=global, ["*"]=todos, ["owner/name",...]=esses, []=nenhum';
