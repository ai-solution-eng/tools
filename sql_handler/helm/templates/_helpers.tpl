{{/*
Expand the name of the chart.
*/}}
{{- define "sqlhandler.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this
(by the DNS naming spec).
*/}}
{{- define "sqlhandler.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "sqlhandler.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "sqlhandler.labels" -}}
helm.sh/chart: {{ include "sqlhandler.chart" . }}
{{ include "sqlhandler.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "sqlhandler.selectorLabels" -}}
app.kubernetes.io/name: {{ include "sqlhandler.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
HPE EZUA / PCAI labels required for platform resource and health monitoring.
*/}}
{{- define "sqlhandler.ezua.labels" -}}
{{- if .Values.ezua.enabled }}
hpe-ezua/app: {{ .Chart.Name }}
hpe-ezua/type: vendor-service
{{- end }}
{{- end }}

{{/*
Create the name of the service account to use.
*/}}
{{- define "sqlhandler.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "sqlhandler.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}