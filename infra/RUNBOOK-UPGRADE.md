# Runbook — upgrade de uma instalação Fintex DSE (topologia A)

Dono: WS-F (plataforma/operações). Cobre o upgrade do chart Helm
(`infra/helm/dse/`) de uma instalação self-hosted no VPC do cliente. **Não
duplica** o runbook de Worker Versioning do Temporal — essa parte é dona do
WS-B e vive em `services/orchestrator/RUNBOOK.md` (WSB-E1-T2); este
documento referencia-o no passo 4 em vez de reescrevê-lo.

## Quando usar este runbook

- Upgrade de versão de imagem de qualquer serviço (`orchestrator`,
  `model-gateway`, `egress-proxy`, adapters, `validation`, `ingest-gateway`).
- Upgrade de versão do próprio chart (`infra/helm/dse/Chart.yaml`
  `version`/`appVersion`).
- Aplicação de uma nova migração de schema (`migrations/000N_*.sql`).
- Rotação de credenciais no secrets backend (Vault) que os serviços
  consomem.

## Pré-requisitos antes de qualquer upgrade

1. `helm lint infra/helm/dse` e `helm template infra/helm/dse` limpos (sem
   erro) — rodar localmente antes de abrir o PR de upgrade.
2. Nenhuma migração pendente sem revisão: `migrations/000N_*.sql` numerada
   corretamente (ver `CONVENTIONS.md`), idempotente
   (`ON CONFLICT DO NOTHING` em `schema_migrations`).
3. Para upgrade de imagem do `orchestrator` (worker Temporal): **PARE aqui e
   siga `services/orchestrator/RUNBOOK.md` (Worker Versioning) antes de
   continuar** — um workflow multi-semana em andamento não pode fazer
   replay contra código incompatível (risco 7 do plano mestre; NFR-09).

## Passo a passo (upgrade de rotina, sem mudança de schema Temporal)

1. **Backup**: snapshot do Postgres (`pg_dump` ou snapshot do volume gerenciado,
   conforme o ambiente) antes de qualquer migração ou upgrade de versão maior.
2. **Migração de schema** (se houver uma nova `migrations/000N_*.sql`):
   ```
   kubectl -n <namespace> exec -it deploy/<release>-dse-orchestrator -- \
     env DSE_DATABASE_URL=$DSE_DATABASE_URL python3 scripts/migrate.py
   ```
   (ou rode `scripts/migrate.py` de um Job/CronJob dedicado — não incluído
   neste chart na Fase 1 porque nenhum serviço steady-state precisa dele
   fora do momento de deploy; considerar adicionar um `helm.sh/hook: pre-upgrade`
   Job quando o número de clientes justificar a automação).
3. **Bump de valores**: atualize `image.tag` do(s) serviço(s) afetado(s) em
   um arquivo de overrides `values-<tenant>.yaml` (nunca edite
   `infra/helm/dse/values.yaml` diretamente para um cliente específico).
4. **Worker Versioning (só se `orchestrator` mudou)**: siga
   `services/orchestrator/RUNBOOK.md` — drain do build id antigo, pin do
   novo build id, cutover controlado. Não prossiga para o passo 5 sem
   confirmar lá que não há workflow em execução no build id que está saindo.
5. **Upgrade**:
   ```
   helm upgrade <release> infra/helm/dse -f values-<tenant>.yaml \
     --namespace <namespace> --atomic --timeout 5m
   ```
   `--atomic` faz rollback automático se o upgrade falhar (readiness probe
   não fica healthy dentro do timeout).
6. **Verificação pós-upgrade**:
   - `kubectl -n <namespace> get pods` — todos `Running`/`Ready`.
   - Checar `otel-collector` (WSF-E7-T1) recebendo spans dos serviços
     atualizados (nenhum gap de telemetria pós-cutover).
   - Rodar um WorkItem de smoke-test de ponta a ponta (Slack/GitHub →
     merge) antes de declarar o upgrade concluído.
7. **Rollback** (se o passo 6 falhar):
   ```
   helm rollback <release> --namespace <namespace>
   ```
   Se a migração de schema do passo 2 não for reversível (ex.: uma coluna
   `NOT NULL` nova sem default), o rollback do Helm sozinho NÃO desfaz o
   schema — migrações neste monorepo devem ser sempre aditivas/compatíveis
   com a versão anterior do código por pelo menos um ciclo de release
   (regra geral de "expand/contract" — expandir o schema num release,
   migrar o código, só então contrair num release seguinte).

## Rotação de secrets (Vault)

Ver `services/platform/dse_secrets/client.py` — `SecretsClient.put_secret`
cria uma nova versão (KV v2 mantém histórico). Procedimento:

1. `put_secret(path, novo_valor)` — não invalida a versão anterior
   automaticamente.
2. Redeploy (rolling restart) dos pods que leem aquele secret via
   ExternalSecret (o ESO atualiza o K8s Secret dentro de
   `secrets.externalSecrets.refreshInterval`; force um `kubectl rollout
   restart` se precisar da rotação imediata).
3. Após confirmar que nenhum pod ainda usa a versão antiga (`vault kv
   metadata get <path>` mostra a versão anterior sem leituras recentes),
   revogue-a: `client.delete_secret(path)` (soft-delete, preserva
   auditoria).

## Referências

- `services/orchestrator/RUNBOOK.md` (WS-B) — Worker Versioning e
  drain-and-cutover detalhado (dono exclusivo desse conteúdo).
- `infra/OSS-BOM.md` — licenças dos componentes atualizados a cada upgrade
  de versão de imagem base.
- `infra/ALERTING-RULES.md` — alertas que devem estar silenciosos/verdes
  antes de declarar o upgrade bem-sucedido.
