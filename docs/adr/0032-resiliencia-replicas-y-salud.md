# ADR 0032 — Resiliencia: réplicas de identity-api, salud y conmutación por error

| Campo | Valor |
| :--- | :--- |
| **Estado** | Propuesta |
| **Fecha** | 2026-11-23 |
| **Dominios afectados** | Infraestructura (`docker-compose.yml`, `gateway/Caddyfile`, `scripts/observability_smoke.py`) |

## Contexto
El Laboratorio 6 ([resultados](../resultados-rendimiento-lab6.md), rama `chore/lab6-performance`) midió que el cuello de botella de TINT es **identity-api**: un solo proceso Node que llega a 108-128 % de CPU en todas las pruebas. Como todo `/api/*` y `/ws/*` protegido pasa por `forward_auth` hacia identity-api, cuando se satura (o se cae) se degradan también los demás servicios: el proxy hacia identity-api degrada desde 500 VUs mientras Caddy solo aguanta 2000 sin degradarse. Además, varios servicios de larga vida no se reiniciaban solos (`web`, `sync-core`, `social-core`, `voice-core`, `stream-core`, las bases…) y ninguno tenía un tope de memoria: un servicio desbocado podía tumbar el host entero.

## Decisión
1. **identity-api con réplicas** (`deploy.replicas: ${IDENTITY_API_REPLICAS:-2}`). Es un servicio sin estado propio (sesiones, revocación y datos viven en PostgreSQL y Redis), así que escala horizontalmente. Las réplicas no tienen `container_name` ni puerto en el host (en producción ya no lo tenía; en desarrollo se entra por el borde, ADR 0031).
   - **Las migraciones corren una sola vez** en el contenedor de un solo uso `identity-migrate` (misma imagen, `npm run migrate`, solo en la red `data`); las réplicas esperan a que termine (`service_completed_successfully`) y arrancan con `npm start`. Si cada réplica migrara al arrancar, en un volumen vacío chocarían: lo midió el banco (una réplica quedaba unhealthy y `up` fallaba en 1 de 18 arranques).
2. **Caddy reparte y conmuta** con el snippet `(identity_upstream)`, usado por `forward_auth` y por la ruta `/api/*`:
   - `dynamic a {$IDENTITY_API_HOST:identity-api} {$IDENTITY_API_PORT:8081}` (refresco 5 s): una IP por réplica desde el DNS de Docker.
   - `lb_policy least_conn`.
   - Salud **pasiva** (`fail_duration 10s`, `max_fails 3`, `unhealthy_status 5xx`): la réplica que falla sale de rotación 10 s. Caddy no corre salud activa sobre upstreams dinámicos; la activa la da el healthcheck de Compose.
   - **Reintento en otra réplica:** `lb_try_duration 8s` (mayor que el `response_header_timeout` de 5 s) y `lb_try_interval 250ms`.
3. **Corte rápido en todos los upstreams** (`(service_timeout)`): `dial_timeout 5s`, `response_header_timeout 5s` y salud pasiva (`fail_duration 10s`, `max_fails 3`, `unhealthy_status 502 503 504`): un servicio caído responde 503 al instante en vez de acumular esperas.
4. **Salud y arranque ordenado:** todo servicio de larga vida tiene `healthcheck`, `restart: unless-stopped` y `deploy.resources.limits.memory`; los que usan un almacén esperan `condition: service_healthy`; el gateway espera a identity-api sana y el borde a gateway y MinIO sanos.

| Servicio | Tope de memoria |
| :--- | :--- |
| gateway, identity-api (por réplica), postgres, minio, mongodb, media-worker, stream-core, firebase-auth-emulator | 1G |
| reverse-proxy, web, redis, analytics, sync-core, social-core, voice-core, coturn | 512M |
| audit-log | 256M |

Los topes son de seguridad, por encima de los picos medidos en el Laboratorio 6 (gateway 565 MiB a 2000 VUs, identity-api 405 MiB).

## Verificación (imágenes reales, 2026-11-23)
`reverse-proxy` → `gateway` → 2 réplicas de identity-api, 400 peticiones a `GET /api/v1/healthz` cada 25 ms; a los 3 s se detiene una réplica.

| Caso | Antes del ajuste (`lb_try_duration 3s`) | Después (`8s`) |
| :--- | :--- | :--- |
| `docker stop` (SIGTERM) de la réplica 1 | — | **400/400 OK** |
| `docker stop` (SIGTERM) de la réplica 2 | 399 OK, 1 × 503 tras 5 s | **400/400 OK** |
| `docker stop -t 0` (SIGKILL) | 399 OK, 1 × 503 | — |

El 503 era una petición que entró en la réplica mientras se apagaba y se colgó hasta el timeout de 5 s; con 3 s de intentos ya no quedaba tiempo para reintentar en la otra réplica. Con una sola réplica, detenerla deja **todo** `/api/*` y `/ws/*` en 503 hasta que vuelve.

## Alternativas consideradas
| Alternativa | Por qué no |
| :--- | :--- |
| `cluster` de Node dentro de un contenedor | Usa más núcleos, pero si el contenedor cae se cae todo; las réplicas dan además conmutación por error. |
| Salud activa en Caddy con upstreams estáticos | Exige listar cada réplica a mano y no sigue al `--scale`. |
| Topes de CPU | Cambiarían los resultados del banco; se dejan solo los de memoria. |

## Consecuencias
**Positivas**
- Caerse o redeplegar una réplica no corta el servicio (medido).
- La capacidad de identity-api crece con `IDENTITY_API_REPLICAS` (se mide en el banco).
- Un servicio caído responde 503 rápido y no arrastra a los demás.

**Negativas**
- En desarrollo identity-api ya no está en `localhost:8081`: el servidor de Vite debe apuntar al borde (`API_PROXY_TARGET`) o correr identity-api fuera de Compose.
- Pendiente en identity-api: cierre ordenado (dejar de aceptar y drenar al recibir SIGTERM) para no depender del reintento.

## Referencias
- Código: `docker-compose.yml`, `gateway/Caddyfile`, `gateway/tests/test_resilience.py`, `scripts/observability_smoke.py`
- ADR 0020, 0021, 0031; Laboratorio 6; Bass, Clements, Kazman, tácticas de disponibilidad (*Detect faults*, *Recover from faults*: active redundancy, retry, removal from service).
