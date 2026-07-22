-- Fintex DSE — Plano 08 §D — flag de preview efêmero por binding.
-- Marca quais repos são "aplicáveis a preview" (front/back que sobem uma URL);
-- o finalizer só posta o link de preview no PR quando o repo resolvido tem
-- deploys_preview = true. Administrado pelo painel Repos & ROI do console.
-- Migração aditiva e idempotente.

ALTER TABLE repo_bindings ADD COLUMN IF NOT EXISTS deploys_preview BOOLEAN NOT NULL DEFAULT false;
