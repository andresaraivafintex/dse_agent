#!/usr/bin/env bash
set -euo pipefail

CHART_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PILOT_VALUES="${CHART_DIR}/values-pilot.yaml"
PILOT_FIXTURE="${CHART_DIR}/tests/values-pilot-render.yaml"
OUT_DIR="$(mktemp -d)"
trap 'rm -rf "${OUT_DIR}"' EXIT

failures=0

pass() { printf 'ok - %s\n' "$1"; }
bad() { printf 'not ok - %s\n' "$1" >&2; failures=$((failures + 1)); }

expect_fail() {
  local label="$1"
  local expected="$2"
  shift 2
  local output
  if output=$("$@" 2>&1); then
    bad "${label} (renderização deveria falhar)"
    return
  fi
  if grep -Fq -- "${expected}" <<<"${output}"; then
    pass "${label}"
  else
    bad "${label} (mensagem inesperada: ${output})"
  fi
}

strict_template() {
  helm template dse-pilot "${CHART_DIR}" \
    -f "${PILOT_VALUES}" -f "${PILOT_FIXTURE}" "$@"
}

helm lint "${CHART_DIR}" >/dev/null
helm template dse-dev "${CHART_DIR}" -f "${CHART_DIR}/values-dev.yaml" \
  >"${OUT_DIR}/dev.yaml"
pass "development profile lints and renders"

expect_fail "pilot sample refuses placeholders/unverified release" \
  "exige digest sha256 imutável" \
  helm template dse-pilot "${CHART_DIR}" -f "${PILOT_VALUES}"

helm lint "${CHART_DIR}" -f "${PILOT_VALUES}" -f "${PILOT_FIXTURE}" >/dev/null
strict_template >"${OUT_DIR}/pilot.yaml"
pass "test-only verified pilot fixture lints and renders"
python3 "${CHART_DIR}/tests/check_render.py" "${OUT_DIR}/pilot.yaml"
pass "strict render satisfies structural workload/network invariants"

expect_fail "rejects in-process execution" "runtime.inProcess deve ser false" \
  strict_template --set runtime.inProcess=true
expect_fail "rejects local fallback execution" "runtime.localFallbackAllowed deve ser false" \
  strict_template --set runtime.localFallbackAllowed=true
expect_fail "rejects fixture agent substrate" "runtime.agentSubstrate não pode ser fixture" \
  strict_template --set-string runtime.agentSubstrate=fixture
expect_fail "rejects missing sandbox RuntimeClass" "sandbox RuntimeClass real é obrigatório" \
  strict_template --set-string runtime.sandboxRuntimeClassName=
expect_fail "rejects model fixture fallback" "modelGateway.allowFixture deve ser false" \
  strict_template --set modelGateway.allowFixture=true
expect_fail "rejects Worker Versioning disabled" "Worker Versioning deve estar habilitado" \
  strict_template --set orchestrator.workerVersioning.enabled=false
expect_fail "rejects development build id" "buildId deve ser imutável" \
  strict_template --set-string orchestrator.workerVersioning.buildId=dev
expect_fail "rejects Vault managed by this dev chart" "vault.externallyManaged deve ser true" \
  strict_template --set vault.externallyManaged=false
expect_fail "rejects Vault dev mode" "vault.devMode é proibido" \
  strict_template --set vault.devMode=true
expect_fail "rejects ESO disabled" "secrets.externalSecrets.enabled deve ser true" \
  strict_template --set secrets.externalSecrets.enabled=false
expect_fail "rejects cluster-wide secret store" "use SecretStore namespaced" \
  strict_template --set-string secrets.externalSecrets.secretStoreKind=ClusterSecretStore
expect_fail "rejects plaintext database password" "postgres.auth.password deve ficar vazio" \
  strict_template --set-string postgres.auth.password=dse_dev_only
expect_fail "rejects missing egress proxy" "egressProxy.enabled é obrigatório" \
  strict_template --set egressProxy.enabled=false
expect_fail "rejects single-replica egress proxy" "egressProxy.replicas deve ser >= 2" \
  strict_template --set egressProxy.replicas=1
expect_fail "rejects missing credential broker" "credential broker é obrigatório" \
  strict_template --set egressProxy.credentialBroker.enabled=false
expect_fail "rejects fixture credential broker" "credential broker não pode permitir fixture" \
  strict_template --set egressProxy.credentialBroker.allowFixture=true
expect_fail "rejects missing model gateway" "modelGateway.enabled é obrigatório" \
  strict_template --set modelGateway.enabled=false
expect_fail "rejects missing audit ledger" "auditLedger.enabled é obrigatório" \
  strict_template --set auditLedger.enabled=false
expect_fail "rejects missing network containment" "networkPolicy.enabled é obrigatório" \
  strict_template --set networkPolicy.enabled=false
expect_fail "rejects mounted Kubernetes token" "automountServiceAccountToken deve ser false" \
  strict_template --set serviceAccount.automountServiceAccountToken=true
expect_fail "rejects mutable image reference" "exige digest sha256 imutável" \
  strict_template --set-string orchestrator.image.digest=
expect_fail "rejects unverified sandbox isolation" "teste de isolamento do sandbox" \
  strict_template --set pilotReadiness.sandboxIsolationVerified=false
expect_fail "rejects unverified strict egress policy" "não provou política estrita" \
  strict_template --set pilotReadiness.egressStrictPolicyVerified=false
expect_fail "rejects unregistered Temporal build id" "não foi registrado/verificado" \
  strict_template --set pilotReadiness.workerVersioningRegistrationVerified=false
expect_fail "rejects wildcard egress host" "wildcard de egress não é permitido" \
  strict_template --set-string 'egressProxy.allowlist[0].host=*.example.net'

if grep -Eq '^kind: Secret$' "${OUT_DIR}/pilot.yaml"; then
  bad "pilot must not render a plaintext Kubernetes Secret"
else
  pass "pilot renders ExternalSecret without plaintext Secret"
fi
grep -Fq 'kind: ExternalSecret' "${OUT_DIR}/pilot.yaml" \
  && pass "pilot renders ExternalSecret" \
  || bad "pilot did not render ExternalSecret"
grep -Fq 'DSE_SANDBOX_INPROCESS: "0"' "${OUT_DIR}/pilot.yaml" \
  && pass "pilot exports in-process disabled" \
  || bad "pilot did not export in-process disabled"
grep -Fq 'DSE_DEPLOYMENT_PROFILE: "production"' "${OUT_DIR}/pilot.yaml" \
  && pass "pilot selects service-side production preflight" \
  || bad "pilot did not select service-side production preflight"
grep -Fq 'DSE_SANDBOX_LOCAL_FALLBACK_ALLOWED: "0"' "${OUT_DIR}/pilot.yaml" \
  && pass "pilot exports local fallback disabled" \
  || bad "pilot did not export local fallback disabled"
grep -Fq 'DSE_SANDBOX_RUNTIME_CLASS: "gvisor"' "${OUT_DIR}/pilot.yaml" \
  && pass "pilot exports approved sandbox RuntimeClass" \
  || bad "pilot did not export sandbox RuntimeClass"
grep -Fq 'DSE_MODEL_GATEWAY_ALLOW_FIXTURE: "0"' "${OUT_DIR}/pilot.yaml" \
  && pass "pilot exports fixture fallback disabled" \
  || bad "pilot did not export fixture fallback disabled"
grep -Fq 'DSE_WORKER_USE_VERSIONING: "true"' "${OUT_DIR}/pilot.yaml" \
  && pass "pilot exports Worker Versioning enabled" \
  || bad "pilot did not export Worker Versioning enabled"
grep -Fq 'DSE_EGRESS_CREDENTIAL_ALLOW_FIXTURE: "0"' "${OUT_DIR}/pilot.yaml" \
  && pass "pilot exports credential fixtures disabled" \
  || bad "pilot did not export credential fixtures disabled"
grep -Fq '@sha256:' "${OUT_DIR}/pilot.yaml" \
  && pass "pilot uses digest-qualified images" \
  || bad "pilot did not use digest-qualified images"
grep -Fq 'automountServiceAccountToken: false' "${OUT_DIR}/pilot.yaml" \
  && pass "pilot disables service-account token automount" \
  || bad "pilot did not disable service-account token automount"
grep -Fq 'readOnlyRootFilesystem: true' "${OUT_DIR}/pilot.yaml" \
  && pass "pilot applies read-only root filesystem to app containers" \
  || bad "pilot did not apply read-only root filesystem"
grep -Fq 'name: dse-pilot-dse-default-deny-egress' "${OUT_DIR}/pilot.yaml" \
  && pass "pilot renders default-deny egress" \
  || bad "pilot did not render default-deny egress"
grep -Fq 'name: dse-pilot-dse-egress-proxy-external' "${OUT_DIR}/pilot.yaml" \
  && pass "pilot limits external CIDRs to egress-proxy policy" \
  || bad "pilot did not render egress-proxy external policy"

if grep -Eq '/var/run/docker.sock|hostPath:' "${OUT_DIR}/pilot.yaml"; then
  bad "pilot must not mount Docker socket or hostPath"
else
  pass "pilot has no Docker socket or hostPath mount"
fi

if (( failures > 0 )); then
  printf '%d chart profile test(s) failed\n' "${failures}" >&2
  exit 1
fi

printf 'all chart profile tests passed\n'
