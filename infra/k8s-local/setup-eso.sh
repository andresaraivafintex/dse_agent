#!/usr/bin/env bash
# WSF-E2-T3b(b) — External Secrets Operator on the local k3d cluster (Phase 3).
#
# Installs the REAL ESO (helm, PINNED version — P7/NFR-09: upgrading is an
# explicit act, never "latest") on the `dse-preview` cluster created by the
# foundation (infra/k8s-local/setup-k3d-argocd.sh) and configures ADR-28's
# preview secrets path:
#
#   Vault (compose, dse_net) --scoped token--> ClusterSecretStore `dse-vault`
#     --> ExternalSecret in a preview namespace --> materialized k8s Secret
#
# Contract for WS-E (per-PR previews, WSE-E4-T10): reference
#   secretStoreRef: { kind: ClusterSecretStore, name: dse-vault }
# and write the preview secrets into Vault under `secret/dse/preview/<...>`.
# The token ESO uses is SCOPED by policy to that prefix (dse-preview-read)
# — a preview cannot read service secrets (dse/slack, dse/github-app,
# etc.) nor anything from another mount. No root token enters the cluster.
#
# Idempotent: safe to re-run; it skips whatever already exists.
set -euo pipefail

ESO_CHART_VERSION="${DSE_ESO_CHART_VERSION:-2.8.0}"   # pinned (P7)
ESO_NS="external-secrets"
KUBE_CONTEXT="${DSE_KUBE_CONTEXT:-k3d-dse-preview}"
VAULT_ADDR_HOST="${VAULT_ADDR:-http://localhost:8200}"     # as seen from the host (this script)
VAULT_ADDR_CLUSTER="http://vault:8200"                     # as seen from the pods (dse_net network)
VAULT_TOKEN="${VAULT_TOKEN:-${VAULT_DEV_ROOT_TOKEN:-dse_dev_root}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

kubectl config use-context "${KUBE_CONTEXT}" >/dev/null

echo "==> 1/5 helm repo external-secrets"
helm repo add external-secrets https://charts.external-secrets.io >/dev/null 2>&1 || true
helm repo update external-secrets >/dev/null

echo "==> 2/5 ESO chart ${ESO_CHART_VERSION} (namespace ${ESO_NS})"
helm upgrade --install external-secrets external-secrets/external-secrets \
  --namespace "${ESO_NS}" --create-namespace \
  --version "${ESO_CHART_VERSION}" \
  --wait --timeout 300s >/dev/null
kubectl -n "${ESO_NS}" wait --for=condition=Available deployment --all --timeout=300s

echo "==> 3/5 policy + token escopados no Vault (dse-preview-read)"
# Policy: read-only under secret/data|metadata/dse/preview/* — never the whole mount.
curl -sf -X PUT -H "X-Vault-Token: ${VAULT_TOKEN}" \
  -d '{"policy":"path \"secret/data/dse/preview/*\" { capabilities = [\"read\"] }\npath \"secret/metadata/dse/preview/*\" { capabilities = [\"read\", \"list\"] }"}' \
  "${VAULT_ADDR_HOST}/v1/sys/policies/acl/dse-preview-read" >/dev/null

if kubectl -n "${ESO_NS}" get secret dse-vault-token >/dev/null 2>&1; then
  echo "    secret dse-vault-token already exists — skipping mint (use --rotate to replace)"
  if [[ "${1:-}" == "--rotate" ]]; then
    kubectl -n "${ESO_NS}" delete secret dse-vault-token
  fi
fi
if ! kubectl -n "${ESO_NS}" get secret dse-vault-token >/dev/null 2>&1; then
  # Orphan periodic token (renews itself within the period; no infinite TTL).
  ESO_VAULT_TOKEN="$(curl -sf -X POST -H "X-Vault-Token: ${VAULT_TOKEN}" \
    -d '{"policies":["dse-preview-read"],"period":"768h","no_parent":true,"display_name":"eso-dse-preview"}' \
    "${VAULT_ADDR_HOST}/v1/auth/token/create-orphan" | python3 -c 'import json,sys; print(json.load(sys.stdin)["auth"]["client_token"])')"
  kubectl -n "${ESO_NS}" create secret generic dse-vault-token \
    --from-literal=token="${ESO_VAULT_TOKEN}"
  unset ESO_VAULT_TOKEN
fi

echo "==> 4/5 ClusterSecretStore dse-vault (Vault do compose via ${VAULT_ADDR_CLUSTER})"
kubectl apply -f "${SCRIPT_DIR}/eso/cluster-secret-store.yaml"
kubectl wait --for=condition=Ready clustersecretstore/dse-vault --timeout=120s

echo "==> 5/5 exemplo: ExternalSecret num namespace de preview"
# Seeds the example secret into Vault (idempotent — it overwrites the version).
curl -sf -X POST -H "X-Vault-Token: ${VAULT_TOKEN}" \
  -d '{"data":{"database_url":"postgresql://preview:preview@postgres:5432/preview_example","api_key":"preview-example-not-a-real-key"}}' \
  "${VAULT_ADDR_HOST}/v1/secret/data/dse/preview/example" >/dev/null
kubectl apply -f "${SCRIPT_DIR}/eso/example-preview-external-secret.yaml"
kubectl -n dse-preview-example wait --for=condition=Ready externalsecret/preview-app-secrets --timeout=120s

cat <<EOF

OK. ESO ${ESO_CHART_VERSION} installed and syncing.
- ClusterSecretStore: dse-vault (Vault do compose, token escopado a secret/dse/preview/*)
- Exemplo vivo:       kubectl -n dse-preview-example get secret preview-app-secrets -o yaml
- Rotacionar o token: $0 --rotate
- Teste real:         pytest -q services/platform/tests/test_eso_preview_secrets.py
EOF
