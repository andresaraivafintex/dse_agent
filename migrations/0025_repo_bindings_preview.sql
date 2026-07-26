-- Fintex DSE — Plan 08 §D — ephemeral preview flag per binding.
-- Marks which repos are "preview-applicable" (front/back that bring up a URL);
-- the finalizer only posts the preview link on the PR when the resolved repo has
-- deploys_preview = true. Managed from the console's Repos & ROI panel.
-- Additive and idempotent migration.

ALTER TABLE repo_bindings ADD COLUMN IF NOT EXISTS deploys_preview BOOLEAN NOT NULL DEFAULT false;
