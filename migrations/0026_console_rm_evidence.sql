-- Fintex DSE — Plan 08 §D (D5) — projection of the evidence (preview) into the
-- console read model. The console-projector tails `work_item_evidence` and
-- reflects the preview status/URL on work_items_view, so the panel can show the
-- preview link next to the PR. Additive and idempotent.

ALTER TABLE console_rm.work_items_view ADD COLUMN IF NOT EXISTS preview_status TEXT;
ALTER TABLE console_rm.work_items_view ADD COLUMN IF NOT EXISTS preview_url TEXT;
ALTER TABLE console_rm.work_items_view ADD COLUMN IF NOT EXISTS demo_passed BOOLEAN;
ALTER TABLE console_rm.work_items_view ADD COLUMN IF NOT EXISTS video_artifact_key TEXT;
ALTER TABLE console_rm.work_items_view ADD COLUMN IF NOT EXISTS trace_artifact_key TEXT;
