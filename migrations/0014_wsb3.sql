-- Fintex DSE — Fase 3 — WS-B (Orquestracao Temporal)
-- Dono: WS-B. Arquivo reservado (ver CONVENTIONS.md, tabela de migracoes da
-- Fase 3) — nao editar fora do WS-B.
--
-- work_item_evidence: projecao DURAVEL e consultavel do estado do pipeline de
-- evidencia (Fase 3 — preview Argo CD + demo Playwright + visual diff, ADR-26/
-- ADR-27). O audit_log (append-only) guarda o historico completo de cada
-- refresh; esta tabela materializa o estado ATUAL (1 linha por work_item,
-- upsert idempotente pelo workflow) para o queue board (WS-F) e operadores
-- responderem "qual o preview/evidencia mais recente deste PR?" sem varrer o
-- ledger.
--
-- P8: o audit_log continua a fonte da verdade imutavel; toda transicao aqui e
-- espelhada por um dse_audit.emit(...) no workflow (acoes preview_triggered,
-- demo_evidence_completed, visual_diff_completed, evidence_degraded,
-- evidence_skipped_backend_only, evidence_refresh_declined_cap).

CREATE TABLE IF NOT EXISTS work_item_evidence (
    work_item_id        TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    -- PreviewRef.status do contrato: created | skipped_backend_only | degraded
    -- (NULL = pipeline nunca rodou ou falhou antes do trigger_preview retornar)
    preview_status      TEXT,
    preview_url         TEXT,
    demo_passed         BOOLEAN,                -- DemoEvidenceResult.passed
    video_artifact_key  TEXT,                   -- chave no artifact store (Garage, WS-E)
    trace_artifact_key  TEXT,
    visual_baseline_key TEXT,                   -- baseline do visual diff (1o run cria)
    refresh_count       INTEGER NOT NULL DEFAULT 0,  -- refreshes ALEM do initial (ADR-26, capado)
    -- initial | fix_cycle | fix_cycle_ci_red | human_request (debounce ADR-26:
    -- refresh SO por commit que muda comportamento ou pedido humano explicito)
    last_refresh_reason TEXT,
    detail              TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_work_item_evidence_tenant ON work_item_evidence (tenant_id);
CREATE INDEX IF NOT EXISTS idx_work_item_evidence_status ON work_item_evidence (preview_status);

DROP TRIGGER IF EXISTS trg_work_item_evidence_updated_at ON work_item_evidence;
CREATE TRIGGER trg_work_item_evidence_updated_at
    BEFORE UPDATE ON work_item_evidence
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT SELECT, INSERT, UPDATE ON work_item_evidence TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0014_wsb3.sql')
ON CONFLICT DO NOTHING;
