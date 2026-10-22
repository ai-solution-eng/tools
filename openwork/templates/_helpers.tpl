{{- define "openwork-web.escapeDomain" -}}
{{- . | replace "." "\\." -}}
{{- end -}}
