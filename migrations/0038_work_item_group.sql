-- Fintex DSE — multi-repo fan-out.
--
-- One user request that genuinely touches N repositories becomes N work items,
-- one repository each, sharing a group. That shape is not a preference: every
-- table downstream is already keyed by work_item_id, and the three that matter
-- most enforce it as a PRIMARY KEY — wse_pr_tracking (0006:33) can hold exactly
-- one PR per work item, wse_ci_status one CI status, wse_comment_refs one
-- comment per surface. Sandbox paths, Pod names and branch names are all
-- derived from work_item_id alone, deliberately, so any worker can rediscover
-- them after a crash. Making one work item span two repositories would mean
-- dropping those keys and rewriting a 3,500-line workflow; giving each repo its
-- own work item costs a nullable column.
--
-- The group exists only so the two items look like ONE thing to the person who
-- asked: one Slack message carrying both pull requests.
--
-- Additive and idempotent.
ALTER TABLE work_items ADD COLUMN IF NOT EXISTS group_id TEXT;

CREATE INDEX IF NOT EXISTS idx_work_items_group
    ON work_items (group_id) WHERE group_id IS NOT NULL;

COMMENT ON COLUMN work_items.group_id IS
    'Sibling work items born from one request share this id (= the PRIMARY item''s id). NULL means a single-repo item; the effective group is COALESCE(group_id, id).';
