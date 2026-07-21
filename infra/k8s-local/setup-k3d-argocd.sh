#!/usr/bin/env bash
# Fundação K8s local da Fase 3 (adendo 02 §2.2) — cluster k3d + Argo CD.
#
# Por que existe: Argo CD ApplicationSet (previews por PR, WSE-E4-T10),
# kube-janitor (TTL de namespaces) e External Secrets Operator (WSF-E2-T3b)
# não rodam em docker-compose. Este script cria o análogo local do "cluster
# K8s in-VPC" do plano (WSF-E0-T1) para desenvolvimento — produção usa os
# Helm charts de infra/helm/dse contra o cluster real do cliente.
#
# Idempotente: pode rodar de novo; pula o que já existe.
set -euo pipefail

CLUSTER_NAME="${DSE_K3D_CLUSTER:-dse-preview}"
ARGOCD_VERSION="${DSE_ARGOCD_VERSION:-v2.13.3}"  # pinado (P7/NFR-09) — upgrade é ato explícito
ARGOCD_NS="argocd"

echo "==> 1/4 cluster k3d '${CLUSTER_NAME}'"
if k3d cluster list | grep -q "^${CLUSTER_NAME}\b"; then
  echo "    já existe — pulando"
else
  # --network dse_net: o cluster enxerga os serviços do docker-compose da
  # fundação (Vault, Garage, model-gateway) pelo nome do container.
  k3d cluster create "${CLUSTER_NAME}" \
    --servers 1 --agents 1 \
    --network dse_net \
    --k3s-arg "--disable=traefik@server:0" \
    --wait --timeout 180s
fi

echo "==> 2/4 kubecontext"
kubectl config use-context "k3d-${CLUSTER_NAME}" >/dev/null
kubectl wait --for=condition=Ready node --all --timeout=120s

echo "==> 3/4 Argo CD ${ARGOCD_VERSION} (namespace ${ARGOCD_NS})"
kubectl get ns "${ARGOCD_NS}" >/dev/null 2>&1 || kubectl create namespace "${ARGOCD_NS}"
kubectl apply -n "${ARGOCD_NS}" \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml" >/dev/null
echo "    aguardando os deployments do Argo CD ficarem Available..."
kubectl -n "${ARGOCD_NS}" wait --for=condition=Available deployment --all --timeout=420s

echo "==> 4/4 smoke: ApplicationSet controller respondendo"
kubectl -n "${ARGOCD_NS}" get statefulset,deployment -o name

cat <<EOF

OK. Cluster '${CLUSTER_NAME}' pronto com Argo CD ${ARGOCD_VERSION}.
- UI (quando precisar):  kubectl -n ${ARGOCD_NS} port-forward svc/argocd-server 8091:443
- senha admin inicial:   kubectl -n ${ARGOCD_NS} get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
- ESO (WS-F instala):    ./infra/k8s-local/setup-eso.sh  (versão pinada + SecretStore Vault + exemplo — WSF-E2-T3b)
- destruir:              k3d cluster delete ${CLUSTER_NAME}
EOF
