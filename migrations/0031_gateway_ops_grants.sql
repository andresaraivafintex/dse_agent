-- 0031 (WS-D) — missing operational grants for dse_app.
--
-- The first real CI run (dse_app role) exposed:
--   - gateway_kill_switches (0011) was born WITHOUT a grant for dse_app; the
--     proxy hook READS it and the operator CREATES/REMOVES kill switches →
--     SELECT, INSERT, DELETE.
--   - work_item_budgets (0011) had SELECT, INSERT, UPDATE; DELETE was missing
--     (operational cleanup of the budget per work item).
-- It passed locally only because the tests connect as the superuser role dse.
--
-- model_call_ledger is deliberately NOT included here: it is an immutable ledger
-- (0028 revokes UPDATE/DELETE and a trigger only allows 'dse'/break-glass). The
-- tests stopped deleting from it (they use unique IDs) — respecting immutability
-- instead of punching a hole in it. Additive and idempotent.
GRANT SELECT, INSERT, DELETE ON gateway_kill_switches TO dse_app;
GRANT DELETE ON work_item_budgets TO dse_app;
