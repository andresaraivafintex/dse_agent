-- Fintex DSE — Skills por repositório (integração console ⇄ engine).
-- Dono: WS-C. Aditiva e idempotente.
--
-- O console (dse_console_pane, Skill Registry) é o banco central de skills:
-- não existe "install" — o operador TICKA por repo quais skills rodam. O tick
-- vive aqui, em `repo_scope`:
--   NULL           -> skill global (legado/nativa do fase1; serve em qualquer repo)
--   '["*"]'        -> tickada para todos os repos
--   '["o/r", ...]' -> tickada só para esses repos (owner/name)
--   '[]'           -> tickada para nenhum repo (não é servida a runs)
-- A leitura tenant-scoped continua em read_approved_skills (P6 fail-closed);
-- o filtro por repo é aplicado lá. O corpo (SKILL.md) segue em `body` e é
-- materializado em `.claude/skills/<skill_key>/SKILL.md` do workspace do
-- sandbox antes do turno do agente (WSC — skill_files.py).

ALTER TABLE skill_registry ADD COLUMN IF NOT EXISTS repo_scope JSONB DEFAULT NULL;

COMMENT ON COLUMN skill_registry.repo_scope IS
    'Ticks por repo do console: NULL=global, ["*"]=todos, ["owner/name",...]=esses, []=nenhum';
