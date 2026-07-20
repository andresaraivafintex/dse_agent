-- Fintex DSE — Fase 2 — WS-B (Orquestracao Temporal)
-- Dono: WS-B. Arquivo reservado (ver CONVENTIONS.md, tabela de migracoes da
-- Fase 2) — nao editar fora do WS-B.
--
-- plan_approval_gate: registro DURAVEL e consultavel do gate de aprovacao de
-- plano por risk class (WSB-E3-T2/T3). O audit_log (append-only) ja guarda o
-- historico completo de eventos, mas e caro de consultar para responder "quais
-- WorkItems estao AGORA parados esperando aprovacao, e por quem?" — pergunta
-- que o queue board (WS-F, Fase 2) e os operadores fazem o tempo todo. Esta
-- tabela materializa esse estado atual (1 linha por work_item, upsert
-- idempotente pelo workflow) sem substituir o audit ledger.
--
-- P8: continua sendo o audit_log a fonte da verdade imutavel; esta tabela e
-- uma projecao mutavel de conveniencia. Toda transicao aqui e espelhada por
-- um dse_audit.emit(...) na mesma Activity.

CREATE TABLE IF NOT EXISTS plan_approval_gate (
    work_item_id      TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    risk_class        TEXT NOT NULL,                         -- classe EFETIVA (policy.classify_risk)
    -- 'pending'  : estacionado, aguardando SIGNAL_PLAN_APPROVAL
    -- 'approved' : liberado (por humano nomeado OU auto-aprovacao de politica)
    -- 'rejected' : recusado; ver rejection_route
    -- 'blocked'  : cascata de aprovadores VAZIA (jamais auto-aprova por ausencia)
    status            TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'blocked')),
    auto_approved     BOOLEAN NOT NULL DEFAULT false,        -- true = liberado por politica (risco baixo), sem humano
    resolved_approvers JSONB NOT NULL DEFAULT '[]'::jsonb,   -- cascata CODEOWNERS -> access bundle
    decided_by        TEXT,                                  -- principal do humano que decidiu (NULL se auto/pending/blocked)
    rejection_route   TEXT CHECK (rejection_route IN ('re_plan', 're_clarify', 'cancel')),
    justification     TEXT,                                  -- obrigatoria na rejeicao (WSB-E3-T3)
    plan_round        INTEGER NOT NULL DEFAULT 0,            -- qual iteracao do Planner (re_plan incrementa)
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_plan_approval_gate_status ON plan_approval_gate (status);
CREATE INDEX IF NOT EXISTS idx_plan_approval_gate_tenant ON plan_approval_gate (tenant_id);

DROP TRIGGER IF EXISTS trg_plan_approval_gate_updated_at ON plan_approval_gate;
CREATE TRIGGER trg_plan_approval_gate_updated_at
    BEFORE UPDATE ON plan_approval_gate
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT SELECT, INSERT, UPDATE ON plan_approval_gate TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0009_wsb2.sql')
ON CONFLICT DO NOTHING;
