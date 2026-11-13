{{- define "openwork-web.escapeDomain" -}}
{{- . | replace "." "\\." -}}
{{- end -}}
{{- /* Name of the Secret holding the five credential keys (SESSION_SECRET,
       OPENCODE_SERVER_USERNAME, OPENCODE_SERVER_PASSWORD, ADMIN_USERNAME,
       ADMIN_PASSWORD). Set existingAuthSecret to a Secret you manage;
       otherwise the chart renders its own from the values. */ -}}
{{- define "openwork-web.authSecretName" -}}
{{- .Values.existingAuthSecret | default (printf "%s-auth" .Release.Name) -}}
{{- end -}}
