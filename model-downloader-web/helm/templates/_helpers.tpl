{{/*
Expand the chart name.
*/}}
{{- define "model-downloader.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully-qualified app name.
*/}}
{{- define "model-downloader.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Common labels.
*/}}
{{- define "model-downloader.labels" -}}
app.kubernetes.io/name: {{ include "model-downloader.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: model-downloader
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Single-source HPE proxy detection. .Values.hpe_proxies is a boolean:
  - true  (default): corporate proxy + no_proxy env and the httpx TLS bypass
                     needed for the HPE Zscaler MITM proxy.
  - false          : no proxy env vars, TLS verification intact.

It does NOT control kyverno/ezua — those are PCAI app features that are on by
default and toggled via .Values.kyverno.enabled / .Values.ezua.enabled.
*/}}
{{- define "model-downloader.hpeProxiesEnabled" -}}
{{- if hasKey .Values "hpe_proxies" }}{{ .Values.hpe_proxies }}{{ else }}true{{ end -}}
{{- end -}}

{{- define "model-downloader.pcaiEnabled" -}}
{{- if and .Values.pcai (hasKey .Values.pcai "enabled") }}{{ .Values.pcai.enabled }}{{ else }}{{ include "model-downloader.hpeProxiesEnabled" . }}{{ end -}}
{{- end -}}

{{- define "model-downloader.kyvernoEnabled" -}}
{{- if and .Values.kyverno (hasKey .Values.kyverno "enabled") }}{{ .Values.kyverno.enabled }}{{ else }}true{{ end -}}
{{- end -}}

{{- define "model-downloader.ezuaEnabled" -}}
{{- if and .Values.ezua (hasKey .Values.ezua "enabled") }}{{ .Values.ezua.enabled }}{{ else }}true{{ end -}}
{{- end -}}

{{/*
True when the downloader should patch httpx to skip TLS verification (HPE Zscaler
MITM with untrusted certs). Can be disabled via .Values.downloader.hf.verifyTls.
*/}}
{{- define "model-downloader.skipTlsVerification" -}}
{{- if and .Values.downloader.hf (hasKey .Values.downloader.hf "verifyTls") }}{{ not .Values.downloader.hf.verifyTls }}{{ else }}{{ include "model-downloader.hpeProxiesEnabled" . }}{{ end -}}
{{- end -}}
