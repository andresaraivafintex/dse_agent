{{/*
Nome base do chart/release.
*/}}
{{- define "dse.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Labels comuns aplicados a todo recurso — incluem o tenant_id (topologia A:
uma instalação = um tenant) para permitir cost allocation e queries de
observabilidade por tenant sem precisar inspecionar env vars.
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
Labels de seletor para um componente específico (ex.: "orchestrator").
*/}}
{{- define "dse.selectorLabels" -}}
app.kubernetes.io/name: {{ .component }}
app.kubernetes.io/instance: {{ $.Release.Name }}
{{- end -}}

{{/* Retorna "true" para perfis sujeitos aos gates de piloto. */}}
{{- define "dse.strictProfile" -}}
{{- if or (eq .Values.deploymentProfile "pilot") (eq .Values.deploymentProfile "production") -}}true{{- else -}}false{{- end -}}
{{- end -}}

{{/* ServiceAccount sem permissões RBAC e sem token montado por default. */}}
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

{{/* Fragmento comum de PodSpec. Não concede acesso à API Kubernetes. */}}
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

{{- define "dse.appTmpMount" -}}
- name: dse-runtime-tmp
  mountPath: /tmp
{{- end -}}

{{/*
Nome de imagem totalmente qualificado, respeitando global.imageRegistry.
Perfis pilot/production só renderizam imagens por digest sha256 imutável.
Uso: {{ include "dse.image" (dict "root" . "repository" .Values.foo.image.repository "tag" .Values.foo.image.tag "digest" .Values.foo.image.digest) }}
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
{{- fail (printf "image %s: digest deve ter formato sha256:<64 hex>" .repository) -}}
{{- end -}}
{{- printf "%s@%s" $repository $digest -}}
{{- else if $strict -}}
{{- fail (printf "profile %s: image %s exige digest sha256 imutável" .root.Values.deploymentProfile .repository) -}}
{{- else -}}
{{- printf "%s:%s" $repository .tag -}}
{{- end -}}
{{- end -}}

{{/*
Validações de segurança que JSON Schema não expressa bem. O objetivo é
falhar durante lint/template, antes de qualquer chamada ao cluster.
*/}}
{{- define "dse.validateValues" -}}
{{- $profile := .Values.deploymentProfile -}}
{{- $strict := or (eq $profile "pilot") (eq $profile "production") -}}
{{- if $strict -}}
  {{- if or (eq .Values.tenant.id "acme-dev") (hasPrefix "replace" (lower .Values.tenant.id)) -}}
    {{- fail (printf "profile %s: tenant.id deve identificar o tenant real" $profile) -}}
  {{- end -}}
  {{- if .Values.runtime.inProcess -}}
    {{- fail (printf "profile %s: runtime.inProcess deve ser false" $profile) -}}
  {{- end -}}
  {{- if .Values.runtime.localFallbackAllowed -}}
    {{- fail (printf "profile %s: runtime.localFallbackAllowed deve ser false" $profile) -}}
  {{- end -}}
  {{- if or (empty .Values.runtime.agentSubstrate) (eq .Values.runtime.agentSubstrate "fixture") -}}
    {{- fail (printf "profile %s: runtime.agentSubstrate não pode ser fixture" $profile) -}}
  {{- end -}}
  {{- if or (empty .Values.runtime.sandboxRuntimeClassName) (hasPrefix "replace" (lower .Values.runtime.sandboxRuntimeClassName)) -}}
    {{- fail (printf "profile %s: sandbox RuntimeClass real é obrigatório" $profile) -}}
  {{- end -}}
  {{- if .Values.modelGateway.allowFixture -}}
    {{- fail (printf "profile %s: modelGateway.allowFixture deve ser false" $profile) -}}
  {{- end -}}
  {{- if not .Values.orchestrator.workerVersioning.enabled -}}
    {{- fail (printf "profile %s: Worker Versioning deve estar habilitado" $profile) -}}
  {{- end -}}
  {{- if or (empty .Values.orchestrator.workerVersioning.buildId) (eq .Values.orchestrator.workerVersioning.buildId "dev") (hasPrefix "replace" (lower .Values.orchestrator.workerVersioning.buildId)) -}}
    {{- fail (printf "profile %s: orchestrator.workerVersioning.buildId deve ser imutável e não pode ser 'dev'" $profile) -}}
  {{- end -}}
  {{- if not .Values.vault.externallyManaged -}}
    {{- fail (printf "profile %s: vault.externallyManaged deve ser true; o chart só contém Vault dev" $profile) -}}
  {{- end -}}
  {{- if .Values.vault.devMode -}}
    {{- fail (printf "profile %s: vault.devMode é proibido" $profile) -}}
  {{- end -}}
  {{- if not (empty .Values.vault.devRootToken) -}}
    {{- fail (printf "profile %s: vault.devRootToken deve ficar vazio" $profile) -}}
  {{- end -}}
  {{- if not .Values.secrets.externalSecrets.enabled -}}
    {{- fail (printf "profile %s: secrets.externalSecrets.enabled deve ser true" $profile) -}}
  {{- end -}}
  {{- if not (empty .Values.postgres.auth.existingSecret) -}}
    {{- fail (printf "profile %s: postgres.auth.existingSecret não pode contornar o ExternalSecret gerenciado pelo chart" $profile) -}}
  {{- end -}}
  {{- if empty .Values.secrets.externalSecrets.secretStoreRef -}}
    {{- fail (printf "profile %s: External Secrets exige secretStoreRef" $profile) -}}
  {{- end -}}
  {{- if ne .Values.secrets.externalSecrets.secretStoreKind "SecretStore" -}}
    {{- fail (printf "profile %s: use SecretStore namespaced; ClusterSecretStore é amplo demais" $profile) -}}
  {{- end -}}
  {{- if not (hasPrefix "https://" .Values.secrets.vaultAddr) -}}
    {{- fail (printf "profile %s: secrets.vaultAddr deve usar https://" $profile) -}}
  {{- end -}}
  {{- if contains "example" (lower .Values.secrets.vaultAddr) -}}
    {{- fail (printf "profile %s: secrets.vaultAddr ainda contém placeholder" $profile) -}}
  {{- end -}}
  {{- if not (empty .Values.postgres.auth.password) -}}
    {{- fail (printf "profile %s: postgres.auth.password deve ficar vazio; use ESO/Vault" $profile) -}}
  {{- end -}}
  {{- if not .Values.egressProxy.enabled -}}
    {{- fail (printf "profile %s: egressProxy.enabled é obrigatório" $profile) -}}
  {{- end -}}
  {{- if empty .Values.egressProxy.allowlist -}}
    {{- fail (printf "profile %s: egressProxy.allowlist não pode ficar vazia" $profile) -}}
  {{- end -}}
  {{- if lt (int .Values.egressProxy.replicas) 2 -}}
    {{- fail (printf "profile %s: egressProxy.replicas deve ser >= 2" $profile) -}}
  {{- end -}}
  {{- if not .Values.egressProxy.credentialBroker.enabled -}}
    {{- fail (printf "profile %s: credential broker é obrigatório" $profile) -}}
  {{- end -}}
  {{- if .Values.egressProxy.credentialBroker.allowFixture -}}
    {{- fail (printf "profile %s: credential broker não pode permitir fixture" $profile) -}}
  {{- end -}}
  {{- if not .Values.modelGateway.enabled -}}
    {{- fail (printf "profile %s: modelGateway.enabled é obrigatório" $profile) -}}
  {{- end -}}
  {{- if not .Values.auditLedger.enabled -}}
    {{- fail (printf "profile %s: auditLedger.enabled é obrigatório" $profile) -}}
  {{- end -}}
  {{- if not .Values.orchestrator.enabled -}}
    {{- fail (printf "profile %s: orchestrator.enabled é obrigatório" $profile) -}}
  {{- end -}}
  {{- if not .Values.temporal.enabled -}}
    {{- fail (printf "profile %s: temporal.enabled é obrigatório neste chart" $profile) -}}
  {{- end -}}
  {{- if not .Values.postgres.enabled -}}
    {{- fail (printf "profile %s: postgres.enabled é obrigatório enquanto endpoint externo não é suportado" $profile) -}}
  {{- end -}}
  {{- if not .Values.networkPolicy.enabled -}}
    {{- fail (printf "profile %s: networkPolicy.enabled é obrigatório" $profile) -}}
  {{- end -}}
  {{- if empty .Values.networkPolicy.egressProxyExternalCidrs -}}
    {{- fail (printf "profile %s: defina ao menos um CIDR externo exclusivo do egress-proxy" $profile) -}}
  {{- end -}}
  {{- range .Values.networkPolicy.egressProxyExternalCidrs -}}
    {{- if eq .cidr "0.0.0.0/0" -}}
      {{- $except := default (list) .except -}}
      {{- if not (and (has "10.0.0.0/8" $except) (has "127.0.0.0/8" $except) (has "169.254.0.0/16" $except) (has "172.16.0.0/12" $except) (has "192.168.0.0/16" $except)) -}}
        {{- fail (printf "profile %s: CIDR 0.0.0.0/0 deve excluir RFC1918, loopback e 169.254.0.0/16" $profile) -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
  {{- if not .Values.otelCollector.enabled -}}
    {{- fail (printf "profile %s: otelCollector.enabled é obrigatório" $profile) -}}
  {{- end -}}
  {{- if not .Values.serviceAccount.create -}}
    {{- fail (printf "profile %s: serviceAccount.create deve ser true" $profile) -}}
  {{- end -}}
  {{- if .Values.serviceAccount.automountServiceAccountToken -}}
    {{- fail (printf "profile %s: automountServiceAccountToken deve ser false" $profile) -}}
  {{- end -}}
  {{- if not .Values.security.podSecurityContext.runAsNonRoot -}}
    {{- fail (printf "profile %s: podSecurityContext.runAsNonRoot deve ser true" $profile) -}}
  {{- end -}}
  {{- if ne .Values.security.podSecurityContext.seccompProfile.type "RuntimeDefault" -}}
    {{- fail (printf "profile %s: seccompProfile.type deve ser RuntimeDefault" $profile) -}}
  {{- end -}}
  {{- if .Values.security.appContainerSecurityContext.allowPrivilegeEscalation -}}
    {{- fail (printf "profile %s: allowPrivilegeEscalation deve ser false" $profile) -}}
  {{- end -}}
  {{- if not .Values.security.appContainerSecurityContext.readOnlyRootFilesystem -}}
    {{- fail (printf "profile %s: appContainerSecurityContext.readOnlyRootFilesystem deve ser true" $profile) -}}
  {{- end -}}
  {{- if not (has "ALL" .Values.security.appContainerSecurityContext.capabilities.drop) -}}
    {{- fail (printf "profile %s: appContainerSecurityContext deve remover ALL capabilities" $profile) -}}
  {{- end -}}
  {{- $resourceSets := dict "postgres" .Values.postgres.resources "temporal" .Values.temporal.resources "temporal-ui" .Values.temporal.ui.resources "redis" .Values.redis.resources "vault" .Values.vault.resources "egress-proxy" .Values.egressProxy.resources "model-gateway" .Values.modelGateway.resources "orchestrator" .Values.orchestrator.resources "adapter-slack" .Values.adapters.slack.resources "adapter-github" .Values.adapters.github.resources "ingest-gateway" .Values.adapters.ingestGateway.resources "validation" .Values.validation.resources "model-server" .Values.modelServer.resources "otel-collector" .Values.otelCollector.resources -}}
  {{- range $name, $resources := $resourceSets -}}
    {{- if or (empty $resources.requests) (empty $resources.limits) -}}
      {{- fail (printf "profile %s: resources.requests/limits obrigatórios para %s" $profile $name) -}}
    {{- end -}}
  {{- end -}}
  {{- range .Values.egressProxy.allowlist -}}
    {{- if contains "*" .host -}}
      {{- fail (printf "profile %s: wildcard de egress não é permitido (%s)" $profile .host) -}}
    {{- end -}}
    {{- if or (not (hasKey . "port")) (lt (int .port) 1) -}}
      {{- fail (printf "profile %s: host de egress %s exige porta explícita" $profile .host) -}}
    {{- end -}}
  {{- end -}}
  {{- $gates := .Values.pilotReadiness -}}
  {{- if not $gates.sandboxIsolationVerified -}}
    {{- fail (printf "profile %s bloqueado: imagem/runtime ainda não passou o teste de isolamento do sandbox" $profile) -}}
  {{- end -}}
  {{- if not $gates.modelGatewayFailClosedVerified -}}
    {{- fail (printf "profile %s bloqueado: gateway ainda não passou o teste fail-closed sem fixtures" $profile) -}}
  {{- end -}}
  {{- if not $gates.egressStrictPolicyVerified -}}
    {{- fail (printf "profile %s bloqueado: imagem do egress-proxy ainda não provou política estrita" $profile) -}}
  {{- end -}}
  {{- if not $gates.auditLedgerMigrationsVerified -}}
    {{- fail (printf "profile %s bloqueado: migrations/role append-only do ledger ainda não foram verificadas" $profile) -}}
  {{- end -}}
  {{- if not $gates.workerVersioningRegistrationVerified -}}
    {{- fail (printf "profile %s bloqueado: build id ainda não foi registrado/verificado na task queue Temporal" $profile) -}}
  {{- end -}}
{{- end -}}
{{- end -}}
