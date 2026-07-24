-- 0031 (WS-D) — grants operacionais faltantes para dse_app.
--
-- Primeira execução real do CI (role dse_app) expôs:
--   - gateway_kill_switches (0011) nasceu SEM grant p/ dse_app; o hook do
--     proxy LÊ e o operador CRIA/REMOVE kill switches → SELECT, INSERT, DELETE.
--   - work_item_budgets (0011) tinha SELECT, INSERT, UPDATE; falta DELETE
--     (limpeza operacional de budget por work item).
-- Passava local só porque os testes conectam como a role superuser dse.
--
-- model_call_ledger NÃO entra aqui de propósito: é ledger imutável (0028
-- revoga UPDATE/DELETE e um trigger só permite 'dse'/break-glass). Os testes
-- deixaram de deletá-lo (usam IDs únicos) — respeitando a imutabilidade em vez
-- de furá-la. Aditiva e idempotente.
GRANT SELECT, INSERT, DELETE ON gateway_kill_switches TO dse_app;
GRANT DELETE ON work_item_budgets TO dse_app;
