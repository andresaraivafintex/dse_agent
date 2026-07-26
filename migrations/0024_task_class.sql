-- Fintex DSE — Plan 08 §A — task class (deterministic taxonomy).
-- Feeds the ROI (human hours per class) and the "by category" charts in
-- Analytics. Classified at admission by label (GitHub) / issue-type (Jira);
-- no LLM decides (P1). Additive and idempotent migration.

ALTER TABLE work_items ADD COLUMN IF NOT EXISTS task_class TEXT NOT NULL DEFAULT 'chore';

-- Closed vocabulary (defense in depth; the classifier only emits these).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'work_items_task_class_check') THEN
        ALTER TABLE work_items ADD CONSTRAINT work_items_task_class_check
            CHECK (task_class IN (
                'bug_fix', 'feature_small', 'test_coverage',
                'dependency_update', 'docs', 'refactor', 'chore'
            ));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_work_items_task_class ON work_items (task_class);
