-- 0033 — widen the CI vocabulary with `no_ci`.
--
-- "nothing has reported yet" and "nothing will ever report" were the same value
-- (`pending`), so a PR opened against a repo with no CI configured waited
-- forever: GitHub legitimately returns an empty check-run array, the aggregator
-- read it as "still running", and no work item this system produced ever
-- finished. `no_ci` makes the difference expressible.
--
-- DROP + ADD rather than the guarded DO block used in 0021: both constraints
-- ALREADY EXIST, so an `IF NOT EXISTS ... ADD CONSTRAINT` would be a silent
-- no-op and the first `no_ci` write would fail the work item. DROP IF EXISTS
-- followed by ADD is idempotent on its own, and the runner never re-runs an
-- applied file.
--
-- Purely permissive: it only widens what is accepted, so it is safe to apply
-- while the previous image is still running.

ALTER TABLE wse_ci_status DROP CONSTRAINT IF EXISTS wse_ci_status_status_check;
ALTER TABLE wse_ci_status ADD CONSTRAINT wse_ci_status_status_check
    CHECK (status IN ('pending', 'green', 'red', 'no_ci'));

ALTER TABLE work_items DROP CONSTRAINT IF EXISTS work_items_ci_status_check;
ALTER TABLE work_items ADD CONSTRAINT work_items_ci_status_check
    CHECK (ci_status IS NULL OR ci_status IN ('pending', 'green', 'red', 'no_ci'));

INSERT INTO schema_migrations (filename) VALUES ('0033_ci_no_ci_status.sql')
ON CONFLICT DO NOTHING;
