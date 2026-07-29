-- 0034 — let a ledger row point back at the audit row it was derived from.
--
-- Context: the coder's spend never reached model_call_ledger, so the console's
-- cost rollup — computed from that table alone — read $0.50 against $27.91 of
-- real spend. Turns are metered at the source from now on, but the historical
-- ones can only ever be metered retroactively, and audit_log is append-only:
-- those old rows can never gain a ledger_id.
--
-- So the pointer goes the other way. The projector uses it to recognise an
-- audit row that has already been accounted for, which is what keeps a full
-- replay from cursor 0 — the documented DR path — from resurrecting a duplicate
-- run on top of a backfilled ledger row and double-counting the same money.
--
-- Additive and inert: the column stays NULL until a backfill runs, and the
-- partial unique index only constrains rows that actually carry a pointer.

ALTER TABLE model_call_ledger ADD COLUMN IF NOT EXISTS source_audit_id BIGINT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mcl_source_audit_id
    ON model_call_ledger (source_audit_id) WHERE source_audit_id IS NOT NULL;

INSERT INTO schema_migrations (filename) VALUES ('0034_ledger_source_audit_id.sql')
ON CONFLICT DO NOTHING;
