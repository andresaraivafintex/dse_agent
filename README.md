# Fintex DSE — Phase 1 ("Core loop")

Implementation of Phase 1 of the development plan (`../plano-desenvolvimento/00-PLANO-MESTRE.md`
§3): the full cycle Slack/GitHub → clarification → single Coder in a sandbox → L1 → deterministic
PR → human review → human merge, durable (Temporal) and auditable (append-only audit ledger).

**Before touching any code, read [`CONVENTIONS.md`](./CONVENTIONS.md)** — it defines the stack, the
owner of each directory, migration numbering, and the shared ports/contracts.

## Status

See `docs/PHASE1-STATUS.md` (generated after integration) for the full map between the Phase 1
exit criteria (Section 16 of the proposal) and what is implemented, mocked, or blocked on real
credentials/infrastructure (AWS/Bedrock account, registered Slack/GitHub Apps, customer K8s
cluster) that this development environment cannot provision on its own.

## How to run

```
make up        # brings up Postgres, Temporal (+UI), Redis, Vault-dev + each workstream's services
make migrate   # applies migrations/*.sql in order
make install   # pip install -e for every Python package/service
make test      # runs the test suite (requires make up + make migrate first)
make down
```

## Layout

```
packages/contracts/    ConversationEvent, WorkItem, PlanArtifact, gateway contract, mutable comment
packages/dse_audit/    append-only audit ledger client
packages/dse_identity/ platform_user_id -> principal resolution (identity map foundations)
services/adapter-slack/    services/adapter-github/    services/ingest-gateway/       (WS-A)
services/orchestrator/                                                               (WS-B)
services/sandbox-runtime/  services/egress-proxy/                                    (WS-C)
services/model-gateway/                                                              (WS-D)
services/validation/                                                                 (WS-E)
services/platform/                                                                   (WS-F)
migrations/            numbered SQL schema (foundation + 1 file reserved per workstream)
infra/                 Helm/Terraform skeletons for the real deployment in the customer VPC
```
