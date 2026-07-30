-- 0035 — index the cursor scan the console projector runs over audit_log.
--
-- `_project_audit` reads
--     WHERE id > <cursor> AND work_item_id IS NOT NULL ORDER BY id LIMIT 2000
-- every 2s. Nothing can order that: the primary key is (tenant_id, id) and
-- `tenant_id` is unconstrained, so its leading column is useless here. With the
-- cursor in step the batch is tiny and nobody notices; from cursor 0 — the
-- documented DR path, which drain() walks one batch at a time — the plan is a
-- full Seq Scan plus a top-N heapsort, PER BATCH, and it grows with history.
-- Measured in production today: 704 buffers and 23,532 rows sorted to hand back
-- 2,000. The same shape over the existing ts index costs 98 — that is the buy.
--
-- The predicate is not there to shrink the index (only 250 of 23,782 rows have
-- no work_item_id). It is there so the planner discharges the query's own NULL
-- test from the index predicate instead of rechecking every row it returns.
--
-- A plain CREATE INDEX on purpose: PostgreSQL 16 rejects CONCURRENTLY on a
-- partitioned table, and `scripts/migrate.py` runs each file inside one
-- transaction, where CONCURRENTLY is illegal regardless — an online build
-- cannot be expressed as a migration here at all. The price is a ShareLock on
-- audit_log and its partitions for the length of the build: milliseconds
-- against today's 5.6 MB, which is the reason this lands now instead of after
-- the table grows. Once it outgrows a write freeze, the online build is a
-- manual three-step, run OUTSIDE this runner in an autocommit session, once per
-- partition:
--
--   1. CREATE INDEX CONCURRENTLY idx_audit_log_projection_cursor_<partition>
--          ON <partition> (id) WHERE work_item_id IS NOT NULL;
--   2. CREATE INDEX idx_audit_log_projection_cursor
--          ON ONLY audit_log (id) WHERE work_item_id IS NOT NULL;
--   3. ALTER INDEX idx_audit_log_projection_cursor
--          ATTACH PARTITION idx_audit_log_projection_cursor_<partition>;
--
-- Step 2 leaves the parent index INVALID until every partition is attached, and
-- the IF NOT EXISTS below matches on the name alone: a half-finished manual
-- build would be adopted silently, so step 3 has to cover every partition.
--
-- Additive: no column, no grant and no trigger changes, audit_log stays
-- append-only. Partitions created later by create_tenant_audit_partition()
-- inherit the index on creation.

CREATE INDEX IF NOT EXISTS idx_audit_log_projection_cursor
    ON audit_log (id) WHERE work_item_id IS NOT NULL;

INSERT INTO schema_migrations (filename) VALUES ('0035_audit_log_projection_index.sql')
ON CONFLICT DO NOTHING;
