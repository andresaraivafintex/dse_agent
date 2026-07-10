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

{{/*
Nome de imagem totalmente qualificado, respeitando global.imageRegistry.
Uso: {{ include "dse.image" (dict "root" . "repository" .Values.foo.image.repository "tag" .Values.foo.image.tag) }}
*/}}
{{- define "dse.image" -}}
{{- if .root.Values.global.imageRegistry -}}
{{- printf "%s%s:%s" .root.Values.global.imageRegistry .repository .tag -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}
{{- end -}}
