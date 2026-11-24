#!/bin/sh
# Certificado del borde (ADR 0031). En producción se monta uno real en /etc/nginx/certs
# (tls.crt y tls.key). En desarrollo, si falta, se genera uno autofirmado para GATEWAY_HOST.
set -eu

CERT_DIR=/etc/nginx/certs
HOST="${GATEWAY_HOST:-localhost}"
mkdir -p "$CERT_DIR"

if [ ! -s "$CERT_DIR/tls.crt" ] || [ ! -s "$CERT_DIR/tls.key" ]; then
  case "$HOST" in
    *[!0-9.]*) SAN="DNS:$HOST,DNS:localhost,IP:127.0.0.1" ;;
    *) SAN="IP:$HOST,DNS:localhost,IP:127.0.0.1" ;;
  esac
  openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
    -subj "/CN=$HOST" -addext "subjectAltName=$SAN" \
    -keyout "$CERT_DIR/tls.key" -out "$CERT_DIR/tls.crt" >/dev/null 2>&1
  echo "reverse-proxy: certificado autofirmado generado para $HOST" >&2
fi
