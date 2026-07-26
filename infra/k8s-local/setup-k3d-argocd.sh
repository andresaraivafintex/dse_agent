#!/usr/bin/env bash
# Phase 3 local K8s foundation (addendum 02 §2.2) — k3d cluster + Argo CD.
#
# Why it exists: Argo CD ApplicationSet (per-PR previews, WSE-E4-T10),
# kube-janitor (namespace TTL) and External Secrets Operator (WSF-E2-T3b) do
# not run on docker-compose. This script creates the local analogue of the
# plan's "in-VPC K8s cluster" (WSF-E0-T1) for development — production uses the
# Helm charts in infra/helm/dse against the customer's real cluster.
#
# Idempotent: safe to re-run; it skips whatever already exists.
set -euo pipefail

CLUSTER_NAME="${DSE_K3D_CLUSTER:-dse-preview}"
ARGOCD_VERSION="${DSE_ARGOCD_VERSION:-v2.13.3}"  # pinned (P7/NFR-09) — upgrading is an explicit act
ARGOCD_NS="argocd"

echo "==> 1/4 cluster k3d '${CLUSTER_NAME}'"
# Plan 08 §D (D4) — local registry: the worker pushes the PR image via
# localhost:5510 (published port); the cluster nodes pull via
# k3d-dse-registry:5510 (same storage, a name k3d can resolve).
REGISTRY_NAME="${DSE_K3D_REGISTRY:-dse-registry}"
REGISTRY_PORT="${DSE_K3D_REGISTRY_PORT:-5510}"
if k3d registry list | grep -q "k3d-${REGISTRY_NAME}\b"; then
  echo "    registry k3d-${REGISTRY_NAME} already exists — skipping"
else
  k3d registry create "${REGISTRY_NAME}" --port "${REGISTRY_PORT}"
fi

if k3d cluster list | grep -q "^${CLUSTER_NAME}\b"; then
  echo "    already exists — skipping"
else
  # --network dse_net: the cluster can see the foundation's docker-compose
  # services (Vault, Garage, model-gateway) by container name.
  #
  # Plan 08 §D (D3): Traefik ENABLED (it was disabled in Phase 3, when no
  # Ingress was needed) + the loadbalancer's port 80 published on
  # localhost:8081 — the operator's browser hits
  # http://<ns>.preview.localhost:8081 (Ingress by hostname; *.localhost
  # resolves to 127.0.0.1 in modern browsers). The SAME path serves the
  # Cloudflare/VPS tunnel later: the tunnel points at localhost:8081 and only
  # DSE_PREVIEW_EXTERNAL_HOST changes.
  k3d cluster create "${CLUSTER_NAME}" \
    --servers 1 --agents 1 \
    --network dse_net \
    --registry-use "k3d-${REGISTRY_NAME}:${REGISTRY_PORT}" \
    -p "8081:80@loadbalancer" \
    --wait --timeout 180s
fi

echo "==> 2/4 kubecontext"
# Reconcile the kubeconfig ALWAYS (not only on creation): a Docker Desktop
# restart destroys the k3d containers and/or reassigns the loadbalancer's API
# server port, leaving the old context pointing at a dead port
# (e.g. https://0.0.0.0:50640 -> connection refused). Re-running this script
# merges the kubeconfig again with the current port. Finding from the deep
# pre-Phase 4 validation: the cluster is EPHEMERAL dev infra — re-creatable by
# this script, never a persistent dependency. (Production uses the customer's
# real cluster.)
k3d kubeconfig merge "${CLUSTER_NAME}" --kubeconfig-merge-default --kubeconfig-switch-context >/dev/null
kubectl config use-context "k3d-${CLUSTER_NAME}" >/dev/null
kubectl wait --for=condition=Ready node --all --timeout=120s

echo "==> 3/4 Argo CD ${ARGOCD_VERSION} (namespace ${ARGOCD_NS})"
kubectl get ns "${ARGOCD_NS}" >/dev/null 2>&1 || kubectl create namespace "${ARGOCD_NS}"
kubectl apply -n "${ARGOCD_NS}" \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml" >/dev/null
echo "    waiting for the Argo CD deployments to become Available..."
kubectl -n "${ARGOCD_NS}" wait --for=condition=Available deployment --all --timeout=420s

echo "==> 4/4 smoke: ApplicationSet controller responding"
kubectl -n "${ARGOCD_NS}" get statefulset,deployment -o name

cat <<EOF

OK. Cluster '${CLUSTER_NAME}' ready with Argo CD ${ARGOCD_VERSION}.
- UI (when needed):      kubectl -n ${ARGOCD_NS} port-forward svc/argocd-server 8091:443
- initial admin passwd:  kubectl -n ${ARGOCD_NS} get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
- ESO (WS-F installs):   ./infra/k8s-local/setup-eso.sh  (pinned version + Vault SecretStore + example — WSF-E2-T3b)
- destroy:               k3d cluster delete ${CLUSTER_NAME}
EOF
