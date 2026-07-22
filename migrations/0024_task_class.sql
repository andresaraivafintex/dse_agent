-- Fintex DSE — Plano 08 §A — classe da tarefa (taxonomia determinística).
-- Alimenta o ROI (horas humanas por classe) e os gráficos "por categoria" do
-- Analytics. Classificada na admissão por label (GitHub) / issue-type (Jira);
-- nenhum LLM decide (P1). Migração aditiva e idempotente.

ALTER TABLE work_items ADD COLUMN IF NOT EXISTS task_class TEXT NOT NULL DEFAULT 'chore';

-- Vocabulário fechado (defesa em profundidade; o classificador só emite estes).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'work_items_task_class_check') THEN
        ALTER TABLE work_items ADD CONSTRAINT work_items_task_class_check
            CHECK (task_class IN (
                'bug_fix', 'feature_small', 'test_coverage',
                'dependency_update', 'docs', 'refactor', 'chore'
            ));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_work_items_task_class ON work_items (task_class);
