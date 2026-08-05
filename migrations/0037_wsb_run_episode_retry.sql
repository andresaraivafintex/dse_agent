-- Fintex DSE — the diário must survive a retry, and must be deletable.
--
-- Two defects in 0036, both found by adversarial review before the feature
-- shipped to the VPS.
--
-- 1) RETRY LOSES THE NEWER ATTEMPT. 0036 keyed the journal on
--    (work_item_id, outcome) with ON CONFLICT DO NOTHING, which is correct
--    idempotency for a Temporal replay or reset of ONE run — and wrong for a
--    work item that runs TWICE. It does: `ingest_gateway/correlate.py` treats
--    only {done, failed} as terminal, so a comment on an `escalated` item
--    correlates to the existing row, adapter-github promotes an @-mention to
--    task_request, and the dispatcher starts a NEW execution under the same
--    workflow id. The second attempt runs against a different base_sha and can
--    stop for a completely different reason — and the INSERT silently kept the
--    FIRST account forever. The Planner then reads the superseded entry and,
--    worse, stamps it "recorded before the current base" using the staleness
--    key 0036 added precisely so that could not happen.
--
--    Last-writer-wins fixes it without reintroducing the duplicate the index
--    exists to prevent: a replay re-renders a byte-identical digest, so the
--    UPDATE is a content no-op, while a genuine retry replaces the stale
--    account and moves created_at so the reader's recency ranking is true.
--    Widening the key with run_id was the alternative and is worse: a Temporal
--    reset mints a new run_id and would duplicate the row.
--
-- 2) THE ROW CANNOT BE DELETED. run_episode holds a copy of the requester's
--    task text (`title`) and of failure output (`digest`, `provenance`), and
--    0036 granted only SELECT, INSERT. `retention.py` preflights each target
--    with has_table_privilege(..., 'DELETE'), so even once run_episode is added
--    to the retention job it would be skipped — and a subject-deletion request
--    could not be honoured without a migration. Data that cannot be deleted is
--    not a store, it is a leak with a schema.

GRANT UPDATE, DELETE ON run_episode TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0037_wsb_run_episode_retry.sql')
ON CONFLICT DO NOTHING;
