-- English-only convergence for text that already exists in a deployed database.
--
-- The translation sweep rewrote the source of every migration, but a migration
-- that has already run does not run again: `schema_migrations` is keyed on
-- filename alone. So an existing installation kept the Portuguese text while a
-- fresh one got English — the two drift apart on strings a human actually reads.
--
-- What matters here is not tidiness. The trigger message below is raised to
-- whoever tries to mutate the ledger, and the skill bodies are handed to an
-- agent as instructions. Both are read; neither is a comment.
--
-- Idempotent, and safe to run against a fresh database where the source already
-- carries the English text: CREATE OR REPLACE is unconditional, and the UPDATEs
-- match on the old text so they simply affect no rows.

-- ---------------------------------------------------------------------------
-- The append-only guard's own error message (see 0028). Redefining the function
-- leaves every trigger that references it in place — they bind to the name.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION dse_ledger_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_user = 'dse'
       OR current_setting('dse.ledger_maintenance', true) = 'on' THEN
        RETURN COALESCE(NEW, OLD);  -- break-glass: DR/controlled retention/test
    END IF;
    RAISE EXCEPTION
        'ledger % is append-only (plan 08 §F F3): % denied for %',
        TG_TABLE_NAME, TG_OP, current_user
        USING ERRCODE = 'raise_exception',
              HINT = 'compliance ledgers are never UPDATEd or DELETEd by the application role';
END;
$$;

COMMENT ON COLUMN skill_registry.repo_scope IS
    'Per-repo ticks from the console: NULL=global, ["*"]=all, ["owner/name",...]=those, []=none';

-- ---------------------------------------------------------------------------
-- Curated skill seeds (see 0010). These are prompt content: the Planner reads
-- an approved skill's body and passes it to the model, so the language here
-- ends up in the instructions an agent follows.
--
-- Matched on the OLD title so a body a human has since edited is never
-- clobbered — an operator's wording wins over this backfill.
-- ---------------------------------------------------------------------------
UPDATE skill_registry SET
    title = 'Never log a PAN or CVV in the clear',
    body  = 'When touching code that handles card data: never log a full PAN (mask it to first 6 + last 4), '
            'never log a CVV/CVC under any circumstance, and never persist a CVV. Use the tokenizer in the '
            'payments module. Reference: PCI-DSS 3.4/3.2.'
WHERE skill_key = 'pci-dss-logging' AND title = 'Nunca logar PAN/CVV em claro';

UPDATE skill_registry SET
    title = 'Postgres migrations must be additive and reversible',
    body  = 'Every migration must be additive (never DROP COLUMN/TABLE in an online release); use '
            'expand/contract across two releases. Create indexes with CONCURRENTLY, outside a transaction. '
            'Never backfill synchronously in a migration — use a batch job.'
WHERE skill_key = 'migrations-reversible' AND title = 'Migrações Postgres precisam ser reversíveis e aditivas';

UPDATE skill_registry SET
    title = 'Webhook handlers must be idempotent',
    body  = 'Deduplicate on a persisted event_id; use SELECT ... FOR UPDATE SKIP LOCKED in the dispatcher; '
            'never rely on delivery order. Return 2xx only after the outbox row is persisted.'
WHERE skill_key = 'idempotent-webhooks' AND title = 'Handlers de webhook devem ser idempotentes';

UPDATE skill_registry SET
    title = 'Draft skill (the Planner must NOT read this)',
    body  = 'This skill is a draft. It exists so the suite can prove the Planner reads only status=approved.'
WHERE skill_key = 'draft-not-ready' AND title = 'Skill em rascunho (NÃO deve ser lida pelo Planner)';

UPDATE skill_registry SET
    title = 'acme-bank naming convention',
    body  = 'Internal acme-bank modules use the acme_ prefix; never expose internal IDs in public API '
            'responses. This skill belongs to tenant acme-bank and must NOT leak into tenant dev.'
WHERE skill_key = 'acme-naming' AND title = 'Convenção de nomes do acme-bank';

INSERT INTO schema_migrations (filename) VALUES ('0032_english_only_ledger_and_seeds.sql')
ON CONFLICT DO NOTHING;
