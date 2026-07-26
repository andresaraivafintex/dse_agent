# Topology B — everything inside the client's VPC (WSF-E5-T3, Phase 4)

The `infra/helm/dse` chart supports two deployment topologies. This document describes
**topology B** (the stricter one) and, above all, the **operational cost** it implies.

## A vs B in one sentence

- **Topology A** (default, `values.yaml`): one installation = one tenant = one namespace inside the
  client's VPC/K8s. In production it ALLOWS pointing infra components at
  **managed/shared** services — managed Postgres (RDS/CloudSQL), Temporal Cloud, external Vault HA,
  Bedrock via PrivateLink. This is where cost amortizes.
- **Topology B** (`values-topology-b.yaml`): the STRICTEST TIER. **EVERYTHING** runs inside the
  client's VPC, with no external managed or shared dependency — Postgres, Temporal
  (+ console), Vault, **and the model itself** (self-hosted / air-gapped, Tier 2). No data
  (neither control nor inference) leaves the VPC.

Maps onto the Tier 2 data-flow diagram in `infra/THREAT-MODEL.md §3.2`.

## How to render/validate

```bash
# Topology A (base)
helm template dse-acme infra/helm/dse

# Topology B (base + strict overlay)
helm template dse-acme infra/helm/dse \
    -f infra/helm/dse/values.yaml \
    -f infra/helm/dse/values-topology-b.yaml

helm lint infra/helm/dse
helm lint infra/helm/dse -f infra/helm/dse/values-topology-b.yaml
```

(No real `helm install` is required — validation is `lint` + `template` rendering valid
YAML, per the task's acceptance criteria.)

## What changes structurally in B

| Component | Topology A (recommended production) | Topology B (strict) |
|---|---|---|
| Postgres | may be managed RDS/CloudSQL | in-cluster StatefulSet mandatory; PITR is the client's responsibility inside the VPC |
| Temporal | may be Temporal Cloud | self-hosted in-cluster (+ UI in-VPC) |
| Vault | may be the client's external Vault HA/HSM | in-cluster, `devMode: false`, unsealed by the client |
| Model | Bedrock via PrivateLink (data stays in the VPC via a private endpoint) | **self-hosted in-cluster model server (GPU)** — `modelServer.enabled: true` |
| Egress allowlist | api.github.com, slack.com, *.amazonaws.com | **internal hosts only** (mirrored git/registry); no public host |
| Console (queue board / Temporal UI) | in-VPC | in-VPC (same as A) |
| ESO / NetworkPolicy | optional / on | ESO on + default-deny NetworkPolicy mandatory |

## Operational cost — NFR-08 × N (the main point)

**NFR-08** is the operational cost of keeping a DSE stack standing (compute + storage + the human
effort of operating Postgres/Temporal/Vault/observability/patching). In topology A, much of
that cost **amortizes** because heavy components can be managed (the provider operates
Postgres/Temporal) and/or shared across tenants of the same operator.

In **topology B, nothing amortizes**: each client gets a **complete standalone stack** inside their
own VPC. So for **N clients** on topology B, the operational cost is approximately:

```
total_cost_B  ≈  N × (NFR-08 full self-hosted stack)
```

versus topology A, where:

```
total_cost_A  ≈  N × (NFR-08 lightweight components)  +  fixed_cost(managed/shared services)
```

### Where the multiplication comes from, concretely

Each B installation carries, per client, **undiluted**:

1. **Self-managed Postgres** — there is no RDS team operating it for you. Backup/PITR, major
   version upgrades, tuning and disk monitoring are **× N**.
2. **Self-hosted Temporal** — an orchestration cluster plus its own persistence and UI, operated and
   upgraded (Worker Versioning, see `infra/RUNBOOK-UPGRADE.md`) **× N**.
3. **In-cluster Vault HA** — unseal, rotation, vault DR, **× N** (not one central Vault).
4. **GPU for the air-gapped model server** — the most expensive item. A dedicated GPU (or pool) per
   client, idle outside peaks, **× N**. There is no inference amortization across clients
   (which is exactly the economics of a managed endpoint like Bedrock).
5. **Observability + patching + on-call** — the human cost of operating N isolated stacks grows
   almost linearly; there is no "single pane of glass" by definition of the air gap.

### Business implication (recorded honestly)

- Topology B only pays off for the tier of clients whose regulatory/contractual requirement
  **forbids** any data from leaving the VPC (its reason to exist). For everyone else, topology A with
  PrivateLink (Tier 1) delivers the same inference-data residency at a fraction of the cost.
- Pilot pricing must reflect the multiplication: a topology B client cannot be
  priced as a marginal tenant on a shared stack — they **are** the stack.
- The dedicated GPU is the dominant cost driver and the item with the longest provisioning lead time in
  the client's VPC; start early (it is on the critical path along with the real credentials — addendum 03
  §Part 3).

## State (P8 — honest)

- The packaging (overlay + model-server template) is complete and validated by
  `helm lint` + `helm template` (both topologies render valid YAML).
- The concrete air-gapped `model-server` (image/serving) is **P2** (WSD-E5-T2/T3): the custom
  provider mechanism is already proven (echo provider, `services/model-gateway/tests/test_echo_provider.py`);
  the real serving image does not block the pilot.
- A real `helm install` on a cluster with a GPU device plugin was not executed (out of scope for the
  acceptance criteria and with no GPU hardware in this session). On a cluster without a GPU, leave
  `modelServer.gpu: 0` so that `helm template`/`lint` validate without requiring `nvidia.com/gpu`.
