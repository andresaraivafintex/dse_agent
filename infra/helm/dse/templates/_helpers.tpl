{{/*
Base name of the chart/release.
*/}}
{{- define "dse.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels applied to every resource — they include the tenant_id (topology
A: one installation = one tenant) so that cost allocation and per-tenant
observability queries work without having to inspect env vars.
*/}}
{{- define "dse.labels" -}}
app.kubernetes.io/part-of: dse
app.kubernetes.io/managed-by: {{ .Release.Service }}
dse.tenant: {{ .Values.tenant.id | quote }}
{{- with .Values.global.labels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
Selector labels for a specific component (e.g. "orchestrator").
*/}}
{{- define "dse.selectorLabels" -}}
app.kubernetes.io/name: {{ .component }}
app.kubernetes.io/instance: {{ $.Release.Name }}
{{- end -}}

{{/* Returns "true" for profiles subject to the pilot gates. */}}
{{- define "dse.strictProfile" -}}
{{- if or (eq .Values.deploymentProfile "pilot") (eq .Values.deploymentProfile "production") -}}true{{- else -}}false{{- end -}}
{{- end -}}

{{/* ServiceAccount with no RBAC permissions and no token mounted by default. */}}
{{- define "dse.serviceAccountName" -}}
{{- if .Values.serviceAccount.name -}}
{{- .Values.serviceAccount.name -}}
{{- else -}}
{{- printf "%s-runtime" (include "dse.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "dse.postgresSecretName" -}}
{{- if .Values.postgres.auth.existingSecret -}}
{{- .Values.postgres.auth.existingSecret -}}
{{- else -}}
{{- printf "%s-postgres-credentials" (include "dse.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "dse.podSecurityContext" -}}
{{- toYaml .Values.security.podSecurityContext -}}
{{- end -}}

{{- define "dse.appContainerSecurityContext" -}}
{{- toYaml .Values.security.appContainerSecurityContext -}}
{{- end -}}

{{- define "dse.statefulContainerSecurityContext" -}}
{{- toYaml .Values.security.statefulContainerSecurityContext -}}
{{- end -}}

{{/* Common PodSpec fragment. Grants no access to the Kubernetes API. */}}
{{- define "dse.podDefaults" -}}
serviceAccountName: {{ include "dse.serviceAccountName" . }}
automountServiceAccountToken: {{ .Values.serviceAccount.automountServiceAccountToken }}
securityContext:
  {{- include "dse.podSecurityContext" . | nindent 2 }}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{- define "dse.appPodDefaults" -}}
{{ include "dse.podDefaults" . }}
volumes:
  - name: dse-runtime-tmp
    emptyDir:
      sizeLimit: {{ .Values.security.tmpSizeLimit }}
{{- end -}}

{{/*
POD defaults for the orchestrator WORKER. Same as appPodDefaults, except that
when runtime.sandboxRbac.create is on it uses the worker's DEDICATED SA (the one
holding the RBAC to create/exec/delete sandbox Pods) and DOES mount the token —
only here, not on the global SA (which stays powerless). The strict gate
inspects serviceAccount.automountServiceAccountToken (the global SA), not this
PodSpec.
*/}}
{{- define "dse.workerPodDefaults" -}}
{{- if .Values.runtime.sandboxRbac.create -}}
serviceAccountName: {{ include "dse.fullname" . }}-orchestrator-worker
automountServiceAccountToken: true
securityContext:
  {{- include "dse.podSecurityContext" . | nindent 2 }}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- else -}}
{{ include "dse.podDefaults" . }}
{{- end }}
volumes:
  - name: dse-runtime-tmp
    emptyDir:
      sizeLimit: {{ .Values.security.tmpSizeLimit }}
{{- end -}}

{{- define "dse.appTmpMount" -}}
- name: dse-runtime-tmp
  mountPath: /tmp
{{- end -}}

{{/*
Fully qualified image name, honouring global.imageRegistry.
The pilot/production profiles only render images by immutable sha256 digest.
Usage: {{ include "dse.image" (dict "root" . "repository" .Values.foo.image.repository "tag" .Values.foo.image.tag "digest" .Values.foo.image.digest) }}
*/}}
{{- define "dse.image" -}}
{{- $repository := .repository -}}
{{- if .root.Values.global.imageRegistry -}}
{{- $repository = printf "%s%s" .root.Values.global.imageRegistry .repository -}}
{{- end -}}
{{- $digest := default "" .digest -}}
{{- $strict := or (eq .root.Values.deploymentProfile "pilot") (eq .root.Values.deploymentProfile "production") -}}
{{- if $digest -}}
{{- if not (regexMatch "^sha256:[a-fA-F0-9]{64}$" $digest) -}}
{{- fail (printf "image %s: digest must use the format sha256:<64 hex chars>" .repository) -}}
{{- end -}}
{{- printf "%s@%s" $repository $digest -}}
{{- else if $strict -}}
{{- fail (printf "profile %s: image %s requires an immutable sha256 digest; set the .digest value for this image" .root.Values.deploymentProfile .repository) -}}
{{- else -}}
{{- printf "%s:%s" $repository .tag -}}
{{- end -}}
{{- end -}}

{{/*
Security validations that JSON Schema does not express well. The goal is to
fail during lint/template, before any call reaches the cluster.
*/}}
{{- define "dse.validateValues" -}}
{{- $profile := .Values.deploymentProfile -}}
{{- $strict := or (eq $profile "pilot") (eq $profile "production") -}}
{{- if $strict -}}
  {{- if or (eq .Values.tenant.id "acme-dev") (hasPrefix "replace" (lower .Values.tenant.id)) -}}
    {{- fail (printf "profile %s: tenant.id must identify the real tenant; replace the sample/placeholder value" $profile) -}}
  {{- end -}}
  {{- if .Values.runtime.inProcess -}}
    {{- fail (printf "profile %s: runtime.inProcess must be false; the agent runs in a sandbox Pod, never in-process" $profile) -}}
  {{- end -}}
  {{- if .Values.runtime.localFallbackAllowed -}}
    {{- fail (printf "profile %s: runtime.localFallbackAllowed must be false; there is no local fallback outside the sandbox" $profile) -}}
  {{- end -}}
  {{- if or (empty .Values.runtime.agentSubstrate) (eq .Values.runtime.agentSubstrate "fixture") -}}
    {{- fail (printf "profile %s: runtime.agentSubstrate must not be fixture; set the real agent substrate" $profile) -}}
  {{- end -}}
  {{- if or (empty .Values.runtime.sandboxRuntimeClassName) (hasPrefix "replace" (lower .Values.runtime.sandboxRuntimeClassName)) -}}
    {{- fail (printf "profile %s: a real sandbox RuntimeClass is required; set runtime.sandboxRuntimeClassName (e.g. gvisor)" $profile) -}}
  {{- end -}}
  {{- if .Values.modelGateway.allowFixture -}}
    {{- fail (printf "profile %s: modelGateway.allowFixture must be false; fixture answers are not allowed here" $profile) -}}
  {{- end -}}
  {{- if not .Values.orchestrator.workerVersioning.enabled -}}
    {{- fail (printf "profile %s: Worker Versioning must be enabled; set orchestrator.workerVersioning.enabled=true" $profile) -}}
  {{- end -}}
  {{- if or (empty .Values.orchestrator.workerVersioning.buildId) (eq .Values.orchestrator.workerVersioning.buildId "dev") (hasPrefix "replace" (lower .Values.orchestrator.workerVersioning.buildId)) -}}
    {{- fail (printf "profile %s: orchestrator.workerVersioning.buildId must be immutable and must not be 'dev'; use the release/commit id" $profile) -}}
  {{- end -}}
  {{- if not .Values.vault.externallyManaged -}}
    {{- fail (printf "profile %s: vault.externallyManaged must be true; this chart only ships a dev Vault, so point it at the managed one" $profile) -}}
  {{- end -}}
  {{- if .Values.vault.devMode -}}
    {{- fail (printf "profile %s: vault.devMode is forbidden; set vault.devMode=false" $profile) -}}
  {{- end -}}
  {{- if not (empty .Values.vault.devRootToken) -}}
    {{- fail (printf "profile %s: vault.devRootToken must be left empty" $profile) -}}
  {{- end -}}
  {{- if not .Values.secrets.externalSecrets.enabled -}}
    {{- fail (printf "profile %s: secrets.externalSecrets.enabled must be true; secrets come from ESO/Vault" $profile) -}}
  {{- end -}}
  {{- if not (empty .Values.postgres.auth.existingSecret) -}}
    {{- fail (printf "profile %s: postgres.auth.existingSecret must not bypass the chart-managed ExternalSecret; leave it empty" $profile) -}}
  {{- end -}}
  {{- if empty .Values.secrets.externalSecrets.secretStoreRef -}}
    {{- fail (printf "profile %s: External Secrets requires secrets.externalSecrets.secretStoreRef" $profile) -}}
  {{- end -}}
  {{- if ne .Values.secrets.externalSecrets.secretStoreKind "SecretStore" -}}
    {{- fail (printf "profile %s: use a namespaced SecretStore; ClusterSecretStore is too broad. Set secrets.externalSecrets.secretStoreKind=SecretStore" $profile) -}}
  {{- end -}}
  {{- if not (hasPrefix "https://" .Values.secrets.vaultAddr) -}}
    {{- fail (printf "profile %s: secrets.vaultAddr must use https://" $profile) -}}
  {{- end -}}
  {{- if contains "example" (lower .Values.secrets.vaultAddr) -}}
    {{- fail (printf "profile %s: secrets.vaultAddr still holds a placeholder; point it at the real Vault address" $profile) -}}
  {{- end -}}
  {{- if not (empty .Values.postgres.auth.password) -}}
    {{- fail (printf "profile %s: postgres.auth.password must be left empty; deliver the password through ESO/Vault" $profile) -}}
  {{- end -}}
  {{- if not .Values.egressProxy.enabled -}}
    {{- fail (printf "profile %s: egressProxy.enabled is required; all outbound traffic goes through the proxy" $profile) -}}
  {{- end -}}
  {{- if empty .Values.egressProxy.allowlist -}}
    {{- fail (printf "profile %s: egressProxy.allowlist must not be empty; list the allowed hosts explicitly" $profile) -}}
  {{- end -}}
  {{- if lt (int .Values.egressProxy.replicas) 2 -}}
    {{- fail (printf "profile %s: egressProxy.replicas must be >= 2 so the egress path is not a single point of failure" $profile) -}}
  {{- end -}}
  {{- if not .Values.egressProxy.credentialBroker.enabled -}}
    {{- fail (printf "profile %s: the credential broker is required; set egressProxy.credentialBroker.enabled=true" $profile) -}}
  {{- end -}}
  {{- if .Values.egressProxy.credentialBroker.allowFixture -}}
    {{- fail (printf "profile %s: the credential broker must not allow fixtures; set egressProxy.credentialBroker.allowFixture=false" $profile) -}}
  {{- end -}}
  {{- if not .Values.modelGateway.enabled -}}
    {{- fail (printf "profile %s: modelGateway.enabled is required" $profile) -}}
  {{- end -}}
  {{- if not .Values.auditLedger.enabled -}}
    {{- fail (printf "profile %s: auditLedger.enabled is required" $profile) -}}
  {{- end -}}
  {{- if not .Values.orchestrator.enabled -}}
    {{- fail (printf "profile %s: orchestrator.enabled is required" $profile) -}}
  {{- end -}}
  {{- if not .Values.temporal.enabled -}}
    {{- fail (printf "profile %s: temporal.enabled is required in this chart" $profile) -}}
  {{- end -}}
  {{- if not .Values.postgres.enabled -}}
    {{- fail (printf "profile %s: postgres.enabled is required while an external endpoint is not supported" $profile) -}}
  {{- end -}}
  {{- if not .Values.networkPolicy.enabled -}}
    {{- fail (printf "profile %s: networkPolicy.enabled is required; the namespace runs default-deny" $profile) -}}
  {{- end -}}
  {{- if empty .Values.networkPolicy.egressProxyExternalCidrs -}}
    {{- fail (printf "profile %s: set at least one external CIDR reserved for the egress-proxy in networkPolicy.egressProxyExternalCidrs" $profile) -}}
  {{- end -}}
  {{- range .Values.networkPolicy.egressProxyExternalCidrs -}}
    {{- if eq .cidr "0.0.0.0/0" -}}
      {{- $except := default (list) .except -}}
      {{- if not (and (has "10.0.0.0/8" $except) (has "127.0.0.0/8" $except) (has "169.254.0.0/16" $except) (has "172.16.0.0/12" $except) (has "192.168.0.0/16" $except)) -}}
        {{- fail (printf "profile %s: CIDR 0.0.0.0/0 must list RFC1918, loopback and 169.254.0.0/16 under except" $profile) -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
  {{- if not .Values.otelCollector.enabled -}}
    {{- fail (printf "profile %s: otelCollector.enabled is required" $profile) -}}
  {{- end -}}
  {{- if not .Values.serviceAccount.create -}}
    {{- fail (printf "profile %s: serviceAccount.create must be true; the workloads need their own powerless SA" $profile) -}}
  {{- end -}}
  {{- if .Values.serviceAccount.automountServiceAccountToken -}}
    {{- fail (printf "profile %s: serviceAccount.automountServiceAccountToken must be false; no workload may hold a Kubernetes API token" $profile) -}}
  {{- end -}}
  {{- if not .Values.security.podSecurityContext.runAsNonRoot -}}
    {{- fail (printf "profile %s: security.podSecurityContext.runAsNonRoot must be true" $profile) -}}
  {{- end -}}
  {{- if ne .Values.security.podSecurityContext.seccompProfile.type "RuntimeDefault" -}}
    {{- fail (printf "profile %s: security.podSecurityContext.seccompProfile.type must be RuntimeDefault" $profile) -}}
  {{- end -}}
  {{- if .Values.security.appContainerSecurityContext.allowPrivilegeEscalation -}}
    {{- fail (printf "profile %s: security.appContainerSecurityContext.allowPrivilegeEscalation must be false" $profile) -}}
  {{- end -}}
  {{- if not .Values.security.appContainerSecurityContext.readOnlyRootFilesystem -}}
    {{- fail (printf "profile %s: security.appContainerSecurityContext.readOnlyRootFilesystem must be true" $profile) -}}
  {{- end -}}
  {{- if not (has "ALL" .Values.security.appContainerSecurityContext.capabilities.drop) -}}
    {{- fail (printf "profile %s: security.appContainerSecurityContext must drop ALL capabilities" $profile) -}}
  {{- end -}}
  {{- $resourceSets := dict "postgres" .Values.postgres.resources "temporal" .Values.temporal.resources "temporal-ui" .Values.temporal.ui.resources "redis" .Values.redis.resources "vault" .Values.vault.resources "egress-proxy" .Values.egressProxy.resources "model-gateway" .Values.modelGateway.resources "orchestrator" .Values.orchestrator.resources "adapter-slack" .Values.adapters.slack.resources "adapter-github" .Values.adapters.github.resources "ingest-gateway" .Values.adapters.ingestGateway.resources "dispatcher" .Values.adapters.ingestGateway.dispatcher.resources "validation" .Values.validation.resources "model-server" .Values.modelServer.resources "otel-collector" .Values.otelCollector.resources -}}
  {{- range $name, $resources := $resourceSets -}}
    {{- if or (empty $resources.requests) (empty $resources.limits) -}}
      {{- fail (printf "profile %s: resources.requests and resources.limits are required for %s" $profile $name) -}}
    {{- end -}}
  {{- end -}}
  {{- range .Values.egressProxy.allowlist -}}
    {{- if contains "*" .host -}}
      {{- fail (printf "profile %s: egress wildcards are not allowed (%s); list every host explicitly" $profile .host) -}}
    {{- end -}}
    {{- if or (not (hasKey . "port")) (lt (int .port) 1) -}}
      {{- fail (printf "profile %s: egress host %s requires an explicit port" $profile .host) -}}
    {{- end -}}
  {{- end -}}
  {{- $gates := .Values.pilotReadiness -}}
  {{- if not $gates.sandboxIsolationVerified -}}
    {{- fail (printf "profile %s blocked: the image/runtime has not passed the sandbox isolation test yet; run it, then set pilotReadiness.sandboxIsolationVerified=true" $profile) -}}
  {{- end -}}
  {{- if not $gates.modelGatewayFailClosedVerified -}}
    {{- fail (printf "profile %s blocked: the gateway has not passed the fail-closed test without fixtures yet; run it, then set pilotReadiness.modelGatewayFailClosedVerified=true" $profile) -}}
  {{- end -}}
  {{- if not $gates.egressStrictPolicyVerified -}}
    {{- fail (printf "profile %s blocked: the egress-proxy image has not proven the strict policy yet; run it, then set pilotReadiness.egressStrictPolicyVerified=true" $profile) -}}
  {{- end -}}
  {{- if not $gates.auditLedgerMigrationsVerified -}}
    {{- fail (printf "profile %s blocked: the ledger append-only migrations/role have not been verified yet; verify them, then set pilotReadiness.auditLedgerMigrationsVerified=true" $profile) -}}
  {{- end -}}
  {{- if not $gates.workerVersioningRegistrationVerified -}}
    {{- fail (printf "profile %s blocked: the build id has not been registered/verified on the Temporal task queue yet; register it, then set pilotReadiness.workerVersioningRegistrationVerified=true" $profile) -}}
  {{- end -}}
{{- end -}}
{{/* K8s driver guard — applies to ANY profile (a correctness check, not a pilot
     gate): sandboxDriver=k8s without RBAC or without a route to the API server
     renders green, but provisioning hangs in the cluster (the worker never
     creates the Pod / kubectl is blocked by the default-deny). Fail closed at
     render time. */}}
{{- if eq .Values.runtime.sandboxDriver "k8s" -}}
  {{- if not .Values.runtime.sandboxRbac.create -}}
    {{- fail "runtime.sandboxDriver=k8s requires runtime.sandboxRbac.create=true (the worker needs the RBAC to create the sandbox Pods)" -}}
  {{- end -}}
  {{- if empty .Values.networkPolicy.kubeApiServer.cidrs -}}
    {{- fail "runtime.sandboxDriver=k8s requires a non-empty networkPolicy.kubeApiServer.cidrs (the worker route to the API server; otherwise kubectl is blocked by the default-deny)" -}}
  {{- end -}}
{{- end -}}
{{- end -}}
