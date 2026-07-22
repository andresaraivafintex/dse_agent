-- Fintex DSE — Plano 08 §D (D5) — projeção da evidência (preview) no read
-- model do console. O console-projector tail-eia `work_item_evidence` e reflete
-- o status/URL do preview na work_items_view, para o painel mostrar o link do
-- preview ao lado do PR. Aditiva e idempotente.

ALTER TABLE console_rm.work_items_view ADD COLUMN IF NOT EXISTS preview_status TEXT;
ALTER TABLE console_rm.work_items_view ADD COLUMN IF NOT EXISTS preview_url TEXT;
ALTER TABLE console_rm.work_items_view ADD COLUMN IF NOT EXISTS demo_passed BOOLEAN;
ALTER TABLE console_rm.work_items_view ADD COLUMN IF NOT EXISTS video_artifact_key TEXT;
ALTER TABLE console_rm.work_items_view ADD COLUMN IF NOT EXISTS trace_artifact_key TEXT;
