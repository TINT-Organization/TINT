# Pruebas de rendimiento por usuarios concurrentes (Laboratorio 6)

Curva de rendimiento y **knee** de los endpoints más delicados de TINT, con k6 por usuarios concurrentes (VUs, `stages`) desde un nodo independiente. Reutiliza el banco de medición de [pruebas-seguridad.md](pruebas-seguridad.md): mismo stack aislado `tintbench`, mismo gateway y mismos tokens sintéticos. La diferencia es que el [banco](pruebas-seguridad.md) mide a tasa fija y aquí se sube la concurrencia hasta encontrar dónde se rompe el sistema.

La feature medida usa los cuatro componentes que pide el laboratorio:
- el front-end web, con su SSR en Node;
- el API Gateway (Caddy);
- un microservicio (identity-api);
- una base de datos (PostgreSQL, más Redis).

## 1. Por qué estos endpoints

La línea base ([evidencia-linea-base.md](evidencia-linea-base.md)) dejó tres datos:
- Satura entre 400 y 800 req/s.
- **identity-api es el contenedor que más CPU gasta**: 45 % a 200 req/s y picos del 97 % en ráfaga.
- No hay rate limit.

El código explica por qué:

| Hallazgo | Dónde | Efecto |
| :--- | :--- | :--- |
| `forward_auth` en todo `/api/*` y `/ws/*` | `gateway/Caddyfile` → `GET /api/v1/internal/auth/check` (`services/identity-api/src/middlewares/centralAuth.ts`) | identity-api es **dependencia síncrona de todos los microservicios** (analytics, stream, voice, social y sync). Si se degrada, se degrada todo. |
| Doble autenticación del host | gateway + `requireAuthHost` en la ruta (`middlewares/auth.ts`) | 2 verificaciones de token y **2 `INSERT … ON CONFLICT DO UPDATE` sobre la misma fila de `users`** por petición (`users.repo.ts`). |
| Un solo proceso Node, pool de PG por defecto (10 conexiones), sin límites de recursos | `server.ts`, `db/client.ts` | El techo de identity-api es un núcleo de CPU y 10 conexiones. |
| Columnas sin índice: `rooms.host_id`, `invitations.room_id`, `guest_sessions.room_id`, `videos.uploaded_by` | migraciones de identity-api | `GET /rooms` además lee **todas** las invitaciones y filtra en JS (`invitations.repo.ts`). |
| Sondeos periódicos del cliente | `apps/web/src/hooks/useRoomPlayback.ts`, `useRevocationProbe.ts`, `ParticipantsPanel.tsx` | `GET /playback` cada 5 s mientras se procesa el vídeo y cada 3 s al reconectar; `GET /guests` y `GET /bans` cada 10 s. |

### Pruebas

| # | `TARGET` | Endpoint | Por qué |
| :--- | :--- | :--- | :--- |
| T1 (principal) | `sync-ticket` | `POST /api/v1/rooms/:id/sync-ticket` (host) | Es el que **más dependencia genera** y el que **más sufre**. Es la puerta de los canales de sync, social, voz y transmisión: la web pide uno en cada (re)conexión de WebSocket, y al menos 2 cada vez que el host entra a una sala. Cuesta 4 queries (2 escrituras sobre la misma fila), 2 verificaciones de token y una firma. `--hosts 1` hace que todos los VUs compitan por esa fila. |
| T2 | `playback` | `GET /api/v1/rooms/:id/playback` (invitado) | El endpoint **más consultado periódicamente**; su carga crece con la audiencia. Cuesta 3 queries, 2 `EXISTS` en Redis y hasta 4 firmas S3. Recorre el camino HS256 del `forward_auth`. |
| T3 | `gateway` | `/healthz` (Caddy), `/api/v1/healthz` (proxy sin auth) y `playback` (proxy + `forward_auth`), en la misma iteración | Separa lo que cuesta cada capa del gateway, para saber si el cuello está en Caddy o detrás. |
| T4 | `claim` | `POST /api/v1/invitations/claim` | La **avalancha de invitados** al empezar un evento. Es público, sin rate limit, y escribe en cada petición (`INSERT guest_sessions` + `XADD`). |
| Opcional | `rooms` | `GET /api/v1/rooms` con 300 salas sembradas | Mide el efecto de las consultas sin índice al crecer los datos. |
| Opcional | `ssr` | `GET /invite/:code` | SSR de la web, que consulta identity-api. Mide el front-end. |

## 2. Método

- **Carga.** `bench/k6/lab6.js` usa el ejecutor `ramping-vus`. Para cada nivel (1, 50, 200, 500, 1000 y 2000 VUs) hay una rampa de 5 s y una meseta de 30 s. Cada VU hace una iteración y `sleep(1)`, como la plantilla del laboratorio.
- **Qué entra en la curva.** Solo las peticiones de la meseta se etiquetan `level:<vus>`, así cada punto mide un nivel estable. Las de las rampas no cuentan.
- **Knee.** Es el último nivel antes del primero que falla (`http_req_failed` > 0) o cuyo p95 supera 10 veces el p95 del nivel de 1 VU (`bench/summarize.py`, `find_knee`).
- **Corridas.** Son 3 por prueba, cada una con el stack recreado (`down -v` y `up`), y se reporta la mediana por nivel.
- **Recursos.** `docker stats` del servidor por SSH, y la CPU del portátil desde `/proc/stat`. Si el portátil pasa del 80 %, ese escalón se marca como **posible límite del generador** (la limitación pendiente de la línea base).
- **Referencia.** Con `sleep(1)` y latencias de ~10 ms, N VUs equivalen a unas N req/s. Por eso se espera el knee entre 400 y 800 VUs para los endpoints baratos, y antes para T1.

## 3. Cómo correrlo

```bash
python3 bench/run.py prepare
python3 bench/run.py lab6 --label lab6                       # T3, T2, T1 y T4, 3 corridas cada uno (~1 h)
python3 bench/run.py lab6 --label lab6 --targets rooms,ssr   # opcionales
python3 bench/run.py restore
python3 bench/summarize.py --lab6 bench/results/lab6         # tablas, knee y bench/results/lab6/lab6-curva.png
```

`--smoke` hace una verificación corta: niveles 1 y 5 con mesetas de 10 s.

**En una sola máquina** (`--local`), sin SSH: levanta solo lo que usa la feature medida (gateway, web, identity-api, PostgreSQL, Redis, MinIO y el emulador) en un proyecto de Compose aparte, `tintlab6`, con volúmenes propios; `down -v` no toca los datos del stack de desarrollo. Necesita las imágenes `tint-monorepo-{web,identity-api,firebase-auth-emulator}:latest` (por ejemplo, `docker tag tint-identity-api:latest tint-monorepo-identity-api:latest` después de `make up`). Si `k6` no está instalado, usa la imagen `grafana/k6:2.4.0` con red del host.

```bash
python3 bench/run.py --local --ip 127.0.0.1 lab6 --label lab6-local
```

O un solo escenario con k6 a mano:

```bash
k6 run -e TARGET=playback -e BASE_URL=https://localhost:8443 -e LEVELS=1,5 -e HOLD=10 \
  --summary-export out.json bench/k6/lab6.js
```

La gráfica necesita `matplotlib` en el portátil; sin ella, el resumen sale igual pero solo como tabla.

## 4. Resultados

Corrida del 2026-11-16 en una sola máquina: [resultados-rendimiento-lab6.md](resultados-rendimiento-lab6.md). Knee de sync-ticket en 50 VUs y de playback, claim y el gateway con identity-api detrás en 200 VUs; Caddy solo no se degrada hasta 2000 VUs. El cuello es identity-api, un solo proceso Node que satura su núcleo.

## 5. Limitaciones

- Los tokens de host son `alg: none` del Firebase Auth Emulator, así que no pagan la verificación RS256 de producción. En producción, T1, `rooms` y todas las rutas de host cuestan más.
- k6 no verifica el certificado interno de Caddy.
- `sleep(1)` hace que la carga dependa de la latencia (modelo cerrado). Cuando el sistema se vuelve lento, baja el req/s ofrecido. Por eso se reportan juntos req/s y latencia por nivel.
