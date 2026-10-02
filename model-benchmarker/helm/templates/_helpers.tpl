{{/*
  Fleet helper pair (SECURE defaults):
  - pcaiEnabled: .Values.pcai.enabled when explicitly set, else FALSE. PCAI
    features (ezua/kyverno-style integration) no longer follow proxy config —
    sites must set pcai.enabled explicitly.
  - Proxy env is wired per-key from the top-level proxy: dict (proxy.http /
    proxy.https / proxy.noProxy) — each key is injected only when non-empty; a
    direct-egress site leaves proxy: {} and no proxy env renders anywhere.
    There is no proxy-detection flag anymore.
*/}}
{{- define "model-benchmarker.pcaiEnabled" -}}
{{- if and .Values.pcai (hasKey .Values.pcai "enabled") }}{{ .Values.pcai.enabled }}{{ else }}false{{ end -}}
{{- end -}}
