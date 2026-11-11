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
Fleet-convention helpers (SECURE defaults):

- pcaiEnabled: .Values.pcai.enabled when explicitly set, else FALSE. PCAI
  features (ezua/kyverno-style integration) no longer follow proxy config —
  sites must set pcai.enabled explicitly.
- Proxy env is wired per-key from the top-level proxy: dict (proxy.http /
  proxy.https / proxy.noProxy) — each key is injected only when non-empty; a
  direct-egress site leaves proxy: {} and no proxy env renders anywhere.
  There is no proxy-detection flag anymore.
*/}}
{{- define "model-downloader.pcaiEnabled" -}}
{{- if and .Values.pcai (hasKey .Values.pcai "enabled") }}{{ .Values.pcai.enabled }}{{ else }}false{{ end -}}
{{- end -}}

{{- define "model-downloader.kyvernoEnabled" -}}
{{- if and .Values.kyverno (hasKey .Values.kyverno "enabled") }}{{ .Values.kyverno.enabled }}{{ else }}true{{ end -}}
{{- end -}}

{{- define "model-downloader.ezuaEnabled" -}}
{{- if and .Values.ezua (hasKey .Values.ezua "enabled") }}{{ .Values.ezua.enabled }}{{ else }}true{{ end -}}
{{- end -}}

{{/*
True when the downloader should patch httpx to skip TLS verification. SECURE
default: verify ON (false) — TLS verification is bypassed only with an
explicit downloader.hf.verifyTls: false, which a site should set false ONLY
behind a corporate MITM proxy with an untrusted cert.
*/}}
{{- define "model-downloader.skipTlsVerification" -}}
{{- if and .Values.downloader.hf (hasKey .Values.downloader.hf "verifyTls") }}{{ not .Values.downloader.hf.verifyTls }}{{ else }}false{{ end -}}
{{- end -}}

{{/*
TLS verification for the app pod's catalog refresh-from-GitHub fetch.
SECURE default: true (verify) when catalog.githubVerifyTls is unset. Set
false ONLY behind a corporate MITM proxy with an untrusted cert.
*/}}
{{- define "model-downloader.catalogGithubVerifyTls" -}}
{{- if and .Values.catalog (hasKey .Values.catalog "githubVerifyTls") }}{{ .Values.catalog.githubVerifyTls }}{{ else }}true{{ end -}}
{{- end -}}
