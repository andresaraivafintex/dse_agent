-- Fintex DSE — the diário de bordo: one durable record per finished run.
--
-- WHY A NEW TABLE RATHER THAN skill_episode (0019_wsc4.sql). skill_episode is
-- the governable INPUT to WS-C's skill-promotion pipeline: its rows are keyed by
-- `pattern_key`, counted by `occurrence_n` against a promotion threshold, and
-- constrained to three sources that all mean "a human corrected us". A record of
-- what a run did is none of those things — it is not a candidate for anything,
-- it does not accumulate toward a threshold, and it must exist for runs that
-- FAILED, which a promotion input must never treat as evidence. Overloading the
-- table would have made every promotion query filter for rows it never wanted.
--
-- The two stores stay separate and keep their own meanings: skill_episode
-- answers "should this become a rule?", run_episode answers "what happened last
-- time someone worked this repo?".
--
-- Written by WS-B (the only writer of work-item lifecycle state), read by WS-C's
-- Planner. Append-only in practice: one row per terminal transition, never
-- updated.

CREATE TABLE IF NOT EXISTS run_episode (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    repo           TEXT,
    work_item_id   TEXT NOT NULL,
    -- The terminal status the run reached. `done` is the only success; the rest
    -- are recorded deliberately, because "what not to bother trying here" is the
    -- most useful thing a failed run knows, and a journal that only remembers
    -- successes is the one that misleads.
    --
    -- These four are the whole terminal set of WorkItemStatus. Operator
    -- cancellation is NOT a fifth value: `_finish_cancelled` writes `failed`
    -- (workflows.py), so listing 'cancelled' here would put a value in the
    -- schema that nothing can ever write.
    outcome        TEXT NOT NULL CHECK (outcome IN ('done', 'failed', 'escalated', 'blocked')),
    -- STALENESS KEY. The base commit the run planned against. An episode is a
    -- claim about the repository AT A COMMIT, and the reader compares this
    -- against the base branch it is planning on now. retrieval_documents already
    -- got this wrong once — it computes a content_sha, stores it, and never
    -- compares it (retrieval.py:213 against the ON CONFLICT target at :205), so
    -- its index can neither detect drift nor shrink. A key that is written and
    -- never read is not a key.
    base_sha       TEXT,
    risk_class     TEXT,
    -- The work item's data classification, carried so a future reader can apply
    -- retention or redaction per tenant policy without re-joining work_items.
    data_class     TEXT,
    -- The deterministic digest rendered for a prompt. No model produced this
    -- text; it is a template over fields the workflow already held at the
    -- terminal transition, which is why writing it costs zero tokens.
    digest         TEXT NOT NULL,
    -- Everything the digest was rendered from, so a future reader can re-render
    -- it differently without a migration and without a second write path.
    provenance     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The read path is "the newest episodes for THIS repo in THIS tenant", so the
-- index carries the ordering column too and the top-k never sorts a heap.
CREATE INDEX IF NOT EXISTS idx_run_episode_tenant_repo_recent
    ON run_episode (tenant_id, repo, created_at DESC);

-- One row per work item per terminal transition. A workflow can be reset or
-- replayed, and the write is scheduled from `_set_status`; without this a reset
-- history would duplicate a run's entry and double its weight in any top-k.
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_episode_work_item_outcome
    ON run_episode (work_item_id, outcome);

-- WS-B writes, WS-C reads. No UPDATE: an episode is what happened, and what
-- happened does not change.
GRANT SELECT, INSERT ON run_episode TO dse_app;
GRANT USAGE, SELECT ON SEQUENCE run_episode_id_seq TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0036_wsb_run_episode.sql')
ON CONFLICT DO NOTHING;
