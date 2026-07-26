# Runbook — upgrading a Fintex DSE installation (topology A)

Owner: WS-F (platform/operations). Covers upgrading the Helm chart
(`infra/helm/dse/`) of a self-hosted installation in the client's VPC. It does **not
duplicate** Temporal's Worker Versioning runbook — that part is owned by
WS-B and lives in `services/orchestrator/RUNBOOK.md` (WSB-E1-T2); this
document references it in step 4 instead of rewriting it.

## When to use this runbook

- Image version upgrade of any service (`orchestrator`,
  `model-gateway`, `egress-proxy`, adapters, `validation`, `ingest-gateway`).
- Version upgrade of the chart itself (`infra/helm/dse/Chart.yaml`
  `version`/`appVersion`).
- Applying a new schema migration (`migrations/000N_*.sql`).
- Rotating credentials in the secrets backend (Vault) that the services
  consume.

## Prerequisites before any upgrade

1. `helm lint infra/helm/dse` and `helm template infra/helm/dse` clean (no
   errors) — run locally before opening the upgrade PR.
2. No pending unreviewed migration: `migrations/000N_*.sql` numbered
   correctly (see `CONVENTIONS.md`), idempotent
   (`ON CONFLICT DO NOTHING` on `schema_migrations`).
3. For an `orchestrator` image upgrade (Temporal worker): **STOP here and
   follow `services/orchestrator/RUNBOOK.md` (Worker Versioning) before
   continuing** — a multi-week workflow already in flight cannot
   replay against incompatible code (master plan risk 7; NFR-09).

## Step by step (routine upgrade, no Temporal schema change)

1. **Backup**: snapshot Postgres (`pg_dump` or a managed volume snapshot,
   depending on the environment) before any migration or major version upgrade.
2. **Schema migration** (if there is a new `migrations/000N_*.sql`):
   ```
   kubectl -n <namespace> exec -it deploy/<release>-dse-orchestrator -- \
     env DSE_DATABASE_URL=$DSE_DATABASE_URL python3 scripts/migrate.py
   ```
   (or run `scripts/migrate.py` from a dedicated Job/CronJob — not included
   in this chart in Phase 1 because no steady-state service needs it
   outside deploy time; consider adding a `helm.sh/hook: pre-upgrade`
   Job once the number of clients justifies the automation).
3. **Bump values**: update `image.tag` for the affected service(s) in
   an overrides file `values-<tenant>.yaml` (never edit
   `infra/helm/dse/values.yaml` directly for a specific client).
4. **Worker Versioning (only if `orchestrator` changed)**: follow
   `services/orchestrator/RUNBOOK.md` — drain the old build id, pin the
   new build id, controlled cutover. Do not proceed to step 5 without
   confirming there that no workflow is still running on the outgoing build id.
5. **Upgrade**:
   ```
   helm upgrade <release> infra/helm/dse -f values-<tenant>.yaml \
     --namespace <namespace> --atomic --timeout 5m
   ```
   `--atomic` rolls back automatically if the upgrade fails (readiness probe
   does not go healthy within the timeout).
6. **Post-upgrade verification**:
   - `kubectl -n <namespace> get pods` — all `Running`/`Ready`.
   - Check that `otel-collector` (WSF-E7-T1) is receiving spans from the
     updated services (no telemetry gap after cutover).
   - Run an end-to-end smoke-test WorkItem (Slack/GitHub →
     merge) before declaring the upgrade complete.
7. **Rollback** (if step 6 fails):
   ```
   helm rollback <release> --namespace <namespace>
   ```
   If the step 2 schema migration is not reversible (e.g. a new
   `NOT NULL` column without a default), a Helm rollback alone does NOT undo the
   schema — migrations in this monorepo must always be additive/compatible
   with the previous code version for at least one release cycle
   (the general "expand/contract" rule — expand the schema in one release,
   migrate the code, and only then contract in a following release).

## Secret rotation (Vault)

See `services/platform/dse_secrets/client.py` — `SecretsClient.put_secret`
creates a new version (KV v2 keeps history). Procedure:

1. `put_secret(path, new_value)` — does not invalidate the previous version
   automatically.
2. Redeploy (rolling restart) the pods that read that secret via
   an ExternalSecret (ESO refreshes the K8s Secret within
   `secrets.externalSecrets.refreshInterval`; force a `kubectl rollout
   restart` if you need the rotation to take effect immediately).
3. Once you have confirmed that no pod is still using the old version (`vault kv
   metadata get <path>` shows the previous version with no recent reads),
   revoke it: `client.delete_secret(path)` (soft-delete, preserves the
   audit trail).

## References

- `services/orchestrator/RUNBOOK.md` (WS-B) — Worker Versioning and
  detailed drain-and-cutover (exclusive owner of that content).
- `infra/OSS-BOM.md` — licenses of the components updated on every base
  image version upgrade.
- `infra/ALERTING-RULES.md` — alerts that must be silent/green
  before declaring the upgrade successful.
