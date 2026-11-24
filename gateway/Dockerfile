# Gateway de TINT (ADR 0020, ADR 0028): Caddy oficial compilado con caddy-ratelimit, porque el binario
# estándar no trae límite de tasa. Versiones fijas: caddy-ratelimit v0.1.0 exige Caddy >= 2.8; su rama
# principal exige 2.10 y xcaddy subiría Caddy sin avisar. El Caddyfile se monta desde Compose.
ARG CADDY_VERSION=2.9.1

FROM caddy:${CADDY_VERSION}-builder-alpine AS builder
ARG CADDY_VERSION
RUN xcaddy build v${CADDY_VERSION} \
    --with github.com/mholt/caddy-ratelimit@v0.1.0

FROM caddy:${CADDY_VERSION}-alpine
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
# Falla el build si el módulo no quedó dentro del binario.
RUN caddy list-modules | grep -qx "http.handlers.rate_limit"
