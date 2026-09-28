{{/*
  Fleet helper pair (ModelDownloader convention):
  - hpeProxiesEnabled: .Values.hpe_proxies when present, else TRUE (HPE PCAI
    default: proxy env on — HPE clusters reach the internet through
    hpeproxy.its.hpecorp.net).
  - pcaiEnabled: .Values.pcai.enabled when explicitly set, else falls back to
    hpeProxiesEnabled — force pcai.enabled only to override the flag.
*/}}
{{- define "model-benchmarker.hpeProxiesEnabled" -}}
{{- if hasKey .Values "hpe_proxies" }}{{ .Values.hpe_proxies }}{{ else }}true{{ end -}}
{{- end -}}

{{- define "model-benchmarker.pcaiEnabled" -}}
{{- if and .Values.pcai (hasKey .Values.pcai "enabled") }}{{ .Values.pcai.enabled }}{{ else }}{{ include "model-benchmarker.hpeProxiesEnabled" . }}{{ end -}}
{{- end -}}
