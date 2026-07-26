# VPS pilot (2 vCPU / 8 GB) — Phase 5 runbook (plan 09)

Ready-to-run kit. Prerequisites outside the VPS: repo published on GitHub
(the release builds the images — **never build on the VPS**), a managed Postgres
provisioned (Neon/Supabase/RDS: the reason is PITR backup + restore, not RAM) and
a domain for the webhooks.

## Execution order

1. **Bootstrap** (as root on the VPS): `bash deploy/vps/bootstrap-k3s.sh`
   — 4G swap, pinned k3s, gVisor/runsc + RuntimeClass, helm, sops+age.
2. **Images**: run the `release` workflow (tag `v*`) → download the
   `values-digests` artifact and paste the digests into your values (the strict
   profile refuses an image without a digest).
3. **Secrets (SOPS + age)**: generate with `age-keygen`; create `values-vps-secrets.yaml`
   from `values-vps.example.yaml` (managed Postgres DSN, provider keys,
   Jira/Slack tokens, GitHub App) and encrypt it:
   `sops -e --age <pubkey> -i values-vps-secrets.yaml`. The private key lives
   ONLY on the VPS (`/root/.config/sops/age/keys.txt`).
4. **Migrations**: automatic — the chart runs `scripts/migrate.py` as a hook Job
   on every install/upgrade.
5. **Install**:
   ```bash
   sops -d values-vps-secrets.yaml | helm upgrade --install dse infra/helm/dse \
     -f infra/helm/dse/values-pilot.yaml \
     -f deploy/vps/values-vps.example.yaml \
     -f /dev/stdin
   ```
6. **Webhooks/TLS**: point DNS at the VPS; k3s's traefik + cert-manager
   (or traefik's ACME resolver) issue the certificate; configure the
   Jira/Slack/GitHub webhooks to `https://<domain>/...` — this retires the
   laptop's `.tunnel/`.
7. **gVisor proof + gate promotion**: with the stack up, run the K8s live-proof
   suite pointed at the VPS kube-context with `runtime_class=gvisor`
   (that is `test_k8s_flow_live.py` with `DSE_SANDBOX_RUNTIME_CLASS=gvisor`) plus
   the red-team. ONLY THEN does the release promote `pilotReadiness.*` — the chart
   refuses a pilot without it, by design.
8. **Restore drill**: test the managed Postgres restore ONCE before
   go-live. No tested restore, no go-live.

## Hard rules for the 2 vCPU VPS (plan 09)

- Sandbox concurrency = **1** (mandatory).
- Image builds: **never** on the VPS.
- Guaranteed CPU `requests` for orchestrator/temporal; limits on the sandbox
  pod (~1.0–1.5 vCPU) — anti-starvation for the heartbeat.
- Full suite/red-team run in CI/on the laptop; on the VPS, smoke + canary only.
- The console (`dse_console_pane`) STAYS OUT of this go-live (default-open auth) —
  or goes behind Tailscale/VPN.

## Known pilot limitations that remain

- Temporal `auto-setup` single-node (mature production: Temporal Cloud/cluster).
- `provision_sandbox` clones the target repo host-side (docker runtime); the VPS's
  K8s runtime uses the runner's in-pod bootstrap — cloning a REAL customer repo
  through egress inside the pod is the final Phase 1 item, closed here.

---

## k3s/Helm POC + tunnel (no domain) — executable runbook

Ready state (2026-07-24): K8s sandbox wiring merged and CI-green; images
on GHCR (release v0.1.0-rc.2, 11 images by digest/tag); POC values in
`deploy/vps/values-vps-poc.yaml` with the cluster's real IPs
(apiserver 10.43.0.1, node 172.16.0.4). Access: `ssh dse-vps`.

Order (each step is idempotent):

1. **Namespace + GHCR pull secret** (the images are private):
   ```bash
   kubectl create namespace dse
   kubectl create secret docker-registry ghcr-pull -n dse \
     --docker-server=ghcr.io --docker-username=<user> --docker-password=<GHCR_TOKEN_read:packages>
   ```
   (Token with `read:packages`. Alternative without a secret: import the images into
   k3s with `k3s ctr images import`, as was done for the agent-runner.)

2. **Sandbox isolation BEFORE helm** (the chart puts a Role/RoleBinding in
   `dse-sandboxes`, which this file creates):
   ```bash
   kubectl apply -f deploy/k8s/sandbox-isolation.yaml
   ```

3. **POC secret** (LiteLLM and the orchestrator read from the ENVIRONMENT, NOT from
   Vault — fixed after the assessment). One secret, injected into both pods via
   `extraEnvSecret: dse-poc-secrets` (already in values-vps-poc.yaml):
   ```bash
   kubectl create secret generic dse-poc-secrets -n dse \
     --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
     --from-literal=LITELLM_MASTER_KEY="$MASTER" \
     --from-literal=DSE_LITELLM_MASTER_KEY="$MASTER" \
     --from-literal=DSE_CODER_MODEL="anthropic/claude-haiku" \
     --from-literal=GITHUB_APP_ID="$GITHUB_APP_ID" \
     --from-literal=GITHUB_APP_INSTALLATION_ID="$GITHUB_APP_INSTALLATION_ID" \
     --from-literal=GITHUB_APP_PRIVATE_KEY="$(cat app.pem)"
   ```
   `LITELLM_MASTER_KEY` (gateway) and `DSE_LITELLM_MASTER_KEY` (orchestrator) MUST
   be identical — otherwise the virtual-key mint returns 401 and falls back to the fixture
   (no real code). The values come from the laptop's `secrets.env`.
   **Check:** `kubectl exec deploy/dse-dse-model-gateway -n dse -- sh -c 'echo $ANTHROPIC_API_KEY'` is non-empty.

4. **Install**:
   ```bash
   helm upgrade --install dse infra/helm/dse \
     -f infra/helm/dse/values-dev.yaml -f deploy/vps/values-vps-poc.yaml -n dse
   ```
   Migrations run as a hook Job. Wait for the pods to be `Ready`.

   **One-off, only on the cluster that predates the dispatcher template:** the
   outbox dispatcher used to exist here as a hand-made object created with
   `kubectl`, and Helm refuses to take over a resource it does not own
   (`invalid ownership metadata`). Delete the two hand-made objects once, before
   this upgrade; the chart recreates them (`templates/dispatcher.yaml`):
   ```bash
   kubectl delete deployment dse-dse-dispatcher -n dse --ignore-not-found
   kubectl delete networkpolicy dse-dse-dispatcher-egress -n dse --ignore-not-found
   ```
   Nothing is lost in the gap: unprocessed rows stay in `ingest_events` with
   `processed=false` and the new pod drains them on its first poll.

5. **Wiring verification** (from the adversarial review):
   ```bash
   kubectl exec deploy/dse-dse-orchestrator -n dse -- kubectl get pods -n dse-sandboxes   # <1s (no timeout)
   kubectl auth can-i --as=system:serviceaccount:dse:dse-dse-orchestrator-worker create pods -n dse-sandboxes  # yes
   kubectl auth can-i --as=system:serviceaccount:dse:dse-dse-orchestrator-worker get secrets -n dse-sandboxes   # no
   ```

6. **Tunnel (webhooks, no domain)**: cloudflared on the VPS pointing at the
   ingress/adapters Service → public HTTPS URL. Register the routes
   (`/github/webhook`, `/slack/events`) in the GitHub App/Slack.

7. **Live proof of a real turn under gVisor**: fire a work item against a
   PUBLIC repo (the minimum). Verify: the sandbox Pod with `runtimeClassName: gvisor`
   comes up in dse-sandboxes; `/workspace` holds the cloned code; `.git/config` has no
   `x-access-token`; the Coder edits; a PR is opened. Only then does the release promote
   `pilotReadiness.sandboxIsolationVerified`.

### Fast-follow (does not block the public POC)
- PRIVATE repo: token injection in the egress-proxy (proxy.py) + read-only lock
  (`git-receive-pack`→403); token = `contents:read`; repo derived server-side
  (ignore `X-Dse-Repo`).
- Checkpoint PVC (rebuild recovery; emptyDir does not recover).
- adapter-jira/teams in the chart (today only slack/github/ingest-gateway).
- Strict `pilot` profile (ESO/SOPS, worker versioning, all digests).
