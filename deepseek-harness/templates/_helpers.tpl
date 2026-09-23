{{- define "dsh-web.escapeDomain" -}}
{{- . | replace "." "\\." -}}
{{- end -}}
