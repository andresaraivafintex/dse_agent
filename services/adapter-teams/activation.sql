-- Fintex DSE — adapter Teams: DDL de ATIVAÇÃO (NÃO aplicada nesta sessão).
--
-- Este arquivo NÃO é uma migração numerada e NÃO é aplicado por
-- scripts/migrate.py. Ele documenta as mudanças de FUNDAÇÃO necessárias para
-- ativar o adapter Teams (decisão de negócio/roadmap Fase 4+). Aplicar como
-- parte de uma migração de ativação, junto com o passo de código:
--
--   CÓDIGO (packages/contracts/dse_contracts/conversation_event.py):
--     class Platform(str, Enum):
--         slack = "slack"
--         github = "github"
--         jira = "jira"
--         teams = "teams"          # <-- 1 linha aditiva
--
-- Por que não está aplicado agora: WS-A (Fase 4) não edita packages/* nem as
-- migrações 0001-0019 (convivência com 4 agentes em paralelo). Os CHECKs abaixo
-- vivem na fundação (migração 0001) e mutá-los pega lock em tabelas quentes
-- (work_items/identity_links) usadas por todos — então a ativação é deliberada,
-- não um efeito colateral desta entrega.
--
-- Todas as mudanças são ADITIVAS (só ampliam o conjunto permitido). Idempotentes.

BEGIN;

-- 1. work_items.source aceita 'teams'
ALTER TABLE work_items DROP CONSTRAINT IF EXISTS work_items_source_check;
ALTER TABLE work_items ADD CONSTRAINT work_items_source_check
    CHECK (source IN ('slack', 'github', 'jira', 'teams'));

-- 2. identity_links.platform aceita 'teams' (para resolve_principal('teams', ...))
ALTER TABLE identity_links DROP CONSTRAINT IF EXISTS identity_links_platform_check;
ALTER TABLE identity_links ADD CONSTRAINT identity_links_platform_check
    CHECK (platform IN ('slack', 'github', 'jira', 'teams'));

COMMIT;

-- Após aplicar isto + a linha do enum: /health passa a reportar
-- {"activated": true} e o endpoint /teams/messages passa do 501
-- teams_not_activated para o pipeline completo (correlate/admit) sem nenhuma
-- outra mudança no serviço.
