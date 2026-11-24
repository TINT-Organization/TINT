# Reverse proxy de TINT (ADR 0031): nginx es la única entrada pública (80/443). Termina TLS, envía
# HSTS y las cabeceras de seguridad, limita por IP y sirve /tint-media/* desde MinIO. Detrás queda
# Caddy (gateway) en HTTP :8080. openssl solo sirve para el certificado autofirmado de desarrollo.
FROM nginx:1.27.5-alpine

RUN apk add --no-cache openssl

COPY nginx.conf /etc/nginx/nginx.conf
COPY templates/ /etc/nginx/templates/
COPY snippets/ /etc/nginx/snippets/
COPY docker-entrypoint.d/15-self-signed-cert.sh /docker-entrypoint.d/15-self-signed-cert.sh
RUN chmod +x /docker-entrypoint.d/15-self-signed-cert.sh && rm -f /etc/nginx/conf.d/default.conf
