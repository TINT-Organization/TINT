# Pruebas de seguridad y rendimiento (Prototipo 3)

Mide el costo en rendimiento de cada medida de seguridad con la misma carga, antes y después. Esta página describe el banco de medición (`bench/`), cómo correrlo y los resultados de la línea base. Los escenarios y las medidas a medir están en el documento del Prototipo 3; las medidas se implementan en ramas aparte y se miden con esta misma herramienta.

## Qué se mide

| Grupo | Métricas | Herramienta |
| :--- | :--- | :--- |
| Latencia de la API | p50, p95 y p99 de `http_req_duration` a tasa fija (50 % de la capacidad calibrada) | k6 (`bench/k6/api.js`, ejecutor `constant-arrival-rate`) |
| Capacidad | req/s máximos sostenidos con p95 ≤ 500 ms, errores ≤ 1 % y sin iteraciones perdidas | calibración por escalones en `bench/run.py` |
| Ráfaga | respuestas 429 sobre el total a 300 req/s durante 15 s, de **un solo cliente** (un token, `data.seeded[0]`; [ADR 0028](adr/0028-limites-y-cabeceras-de-seguridad-en-el-gateway.md)) | k6 (`PHASE=burst`) |
| Canal Sync | p99 de broadcast (< 100 ms), p99 de drift (< 500 ms), p95 de `ws_connecting` (< 1 s) | `services/sync-core/loadtest/sync.js` por el gateway |
| Recursos | CPU media y máxima, memoria máxima por contenedor y por fase | `docker stats` por SSH |
| Superficie | puertos abiertos, rutas internas, HSTS, TLS, bases de datos accesibles desde fuera | `nmap`, `curl`, `testssl.sh` (`bench/security_checks.py`) |

Los umbrales del repo no se relajan: si una medida los rompe se documenta la causa.

## Método

- **Dos máquinas.** El portátil genera la carga (ethernet, misma LAN) y el servidor solo corre el stack. La carga va directo al gateway del repo (`tint-gateway`, no a `tint-edge`) por `https://<IP>:8443`.
- **Stack aislado.** El banco corre en un clon aparte (`C:\tint\bench\tint-monorepo`, proyecto de Compose `tintbench`, volúmenes propios), fijado a un commit. Reutiliza las imágenes ya construidas de ese commit (`bench/compose.bench.yml`, `BENCH_IMAGE_TAG`). El stack público se detiene mientras se mide y se vuelve a arrancar al terminar.
- **Modo emulador.** El stack del banco usa `demo-tint` con el Firebase Auth Emulator (ADR 0024) y un `JWT_SECRET` sintético: así k6 fabrica los tokens de host y de invitado sin usar credenciales reales. Solo datos sintéticos (Ley 1581).
- **Certificado.** El gateway usa `GATEWAY_HOST=<IP>` (certificado interno de Caddy) y k6 corre con `--insecure-skip-tls-verify`. Es igual en el "antes" y en el "después".
- **Corridas.** Cada corrida parte de `down -v` y `up`, calienta 30 s sin medir y mide API, ráfaga y Sync por separado. Son 3 corridas y se reporta la mediana con el rango (mín-máx). Una diferencia solo es concluyente si supera la variación entre corridas de ambos lados (`bench/summarize.py`).
- **Ruido.** Antes de cada corrida se comprueba que `transcriptor` (otro compose del servidor) esté en reposo; si no, se descarta.

## Cómo correrlo

```bash
python3 bench/run.py prepare                         # detiene el stack público y prepara el clon aislado
python3 bench/run.py measure --label antes-develop   # calibra y hace 3 corridas
python3 bench/run.py security --label antes-develop  # nmap, curl, testssl.sh
python3 bench/run.py restore                         # baja el banco y devuelve el servidor a como estaba
python3 bench/summarize.py bench/results/antes-develop [bench/results/despues]
```

`--smoke` hace una corrida corta de verificación. Para el "después": `prepare --commit <sha>` sobre la rama con la medida, `build` de sus imágenes con una etiqueta, `--image-tag` en `measure` y `--rate` con la tasa de `calibration.json` del "antes" para comparar a la misma carga. Los resultados crudos quedan en `bench/results/` (ignorado por git); solo se versiona la tabla resumida de esta página.

Desde ADR 0028 el gateway es una imagen propia (`gateway/Dockerfile`, Caddy con `caddy-ratelimit`): en el clon del banco hay que construirla junto con las demás (`docker compose build`, con `BENCH_IMAGE_TAG`), porque `up --no-build` ya no la descarga. La ráfaga de la línea base se repartió entre 20 tokens; desde ADR 0028 sale de uno solo. Sin límite de tasa el resultado es el mismo (0 respuestas 429), así que la línea base sigue sirviendo de "antes".

Requisitos en el portátil: `k6`, `nmap`, `ssh` con alias al servidor y, opcionalmente, `testssl.sh`.

## Resultados: línea base (`develop`, commit 5767874)

El registro de cada prueba aplicada, con los resultados por corrida, está en [evidencia-linea-base.md](evidencia-linea-base.md).

Medida el 2026-11-16 con 3 corridas, sin ninguna medida de seguridad. Servidor: Windows con Docker Desktop (12 CPU, ~7,7 GB para Docker); el portátil genera la carga por ethernet (~1 ms). Cada celda es la mediana y, entre paréntesis, el mínimo y el máximo de las 3 corridas.

### Capacidad y latencia de la API (mezcla de operaciones, tasa fija de 200 req/s)

La calibración sube la tasa por escalones de 30 s: 25, 50, 100, 200 y 400 req/s cumplen (p95 ≤ 26 ms, 0 fallos) y 800 req/s satura (p95 6,3 s, 17 % de fallos, 43 % de iteraciones perdidas). **Capacidad sostenida: 400 req/s**; la fase de latencia usa el 50 % (200 req/s). El "después" debe correr con `--rate 200`.

| Métrica | Línea base |
| :--- | :--- |
| p50 de la API | 9,70 ms (9,69-9,73) |
| p95 de la API | 13,75 ms (13,74-13,95) |
| p99 de la API | 15,40 ms (15,38-16,10) |
| Fallos | 0 % |
| Peticiones por segundo sostenidas | 199 (199-199) |
| Ráfaga 300 req/s a `GET /rooms` durante 15 s: respuestas 2xx / 429 | 4500 / 0 (la ráfaga queda por debajo de la capacidad: p95 20,9 ms) |

### Canal Sync por el gateway (200 conexiones, 60 s)

| Métrica | Línea base | Umbral del repo |
| :--- | :--- | :--- |
| p99 de broadcast | 14 ms (12-23) | < 100 ms |
| p99 de drift | 0 ms | < 500 ms |
| p95 de `ws_connecting` | 135 ms (126-169) | < 1 s |

### Recursos (CPU media / máxima, memoria máxima)

| Contenedor | Carga de API (200 req/s) | Ráfaga (300 req/s) |
| :--- | :--- | :--- |
| gateway | 25 % / 31 %, 28 MiB | 32 % / 48 %, 29 MiB |
| identity-api | 45 % / 61 %, 99 MiB | 58 % / 97 %, 117 MiB |
| postgres | 19 % / 25 %, 64 MiB | 31 % / 46 %, 67 MiB |

Con la carga de Sync, sync-core llega a 3 % de CPU media (27 % máxima) y el gateway a 50 % de CPU máxima durante el handshake de las 200 conexiones.

### Superficie de seguridad (`bench/security_checks.py`, desde la LAN)

| Comprobación | Línea base |
| :--- | :--- |
| Versiones de TLS ofrecidas (`testssl.sh`) | solo TLS 1.2 y 1.3 (Caddy ya lo hace por defecto; no hay protocolos anteriores) |
| Cabecera HSTS | ausente |
| Cabecera `Server` expuesta | sí |
| `POST /api/v1/internal/rooms/x/disable` sin token | **401** (no 404) |
| Misma ruta con token de host | 404 |
| MinIO por HTTP en claro (`:9000`) | accesible |
| Bases de datos y MinIO alcanzables desde la LAN | PostgreSQL 5432, Redis 6379, MongoDB 27017, MinIO 9000 y 9001 |
| Servicios de la app publicados | 5173 y 8080-8086 más el gateway en 8443 |

Dos correcciones al documento del Prototipo 3:

- **Ruta interna:** la fila "404 (ya)" no es exacta. El `forward_auth` corre antes que el `respond 404`, así que sin token la respuesta es 401. Con un token válido sí es 404. Hoy el gateway no esconde la ruta a quien no está autenticado: solo la oculta a quien sí lo está.
- **TLS:** el requisito de TLS 1.2 o superior del escenario 1 ya se cumple en `develop`. La medida real que falta es HSTS (y MinIO en claro), no el protocolo.

### Limitaciones de esta medición

- No se midió la CPU del portátil durante las corridas. El escalón de 800 req/s puede estar limitado por el generador y no por el servidor; la capacidad de 400 req/s es por tanto un mínimo fiable para la comparación a 200 req/s, no un máximo del sistema.
- El certificado es interno de Caddy y k6 no lo verifica; `localhost` se resuelve a la IP del servidor. Es igual en el "antes" y el "después".
- Los puertos de la tabla de superficie incluyen servicios propios de la máquina de pruebas (SSH, Windows y otros) que no son de TINT.
- El servidor corre además `transcriptor` (uso personal); estaba en reposo (0,1 % de CPU) antes de cada corrida.

## Resultados: escenario 1, canal seguro (antes y después de HSTS)

Medida el 2026-11-23 sobre el stack local de `docker compose`, con la misma carga y la misma máquina en ambos lados. Lo único que cambia entre el "antes" y el "después" es el `gateway/Caddyfile`: el "antes" usa el de `c262cbd` (sin HSTS) y el "después" el de `develop` con la medida ([ADR 0029](adr/0029-hsts-y-canal-seguro-en-el-gateway.md), PR #179). Tras el merge, `develop` ya trae HSTS, así que un "antes" nuevo debe armarse con `git checkout c262cbd -- gateway/Caddyfile`.

| Prueba | Antes | Después |
| :--- | :--- | :--- |
| Peticiones en claro que llegan a los vídeos (`curl http://localhost:9000/minio/health/live`) | 200 en HTTP plano | **200 en HTTP plano (no cumple: excepción documentada, ver abajo)** |
| Versión mínima de TLS aceptada (`testssl.sh` 3.2) | TLS 1.2 y 1.3; TLS 1.0 y 1.1 rechazados | TLS 1.2 o superior; SSLv2, SSLv3, TLS 1.0 y 1.1 "not offered" |
| HSTS activado (`curl -I` y `testssl.sh`) | no | sí: `max-age=31536000; includeSubDomains; preload` (365 días) |
| Redirección HTTP a HTTPS | 308 | 308, sin HSTS en la respuesta HTTP (el navegador la ignora por RFC 6797) |
| p95 de `ws_connecting` por el gateway (k6; umbral del repo: < 1 s) | 144,18 ms (133,74-159,98) | 137,09 ms (123,77-188,26) |

### Canal Sync por el gateway, antes y después

`bench/k6/sync-gateway.js` con 50 conexiones durante 30 s, 3 corridas por lado; cada celda es la mediana y, entre paréntesis, el mínimo y el máximo. Ambos lados usan el mismo calentamiento sin medir (5 conexiones, 10 s).

| Métrica | Antes | Después | Umbral del repo |
| :--- | :--- | :--- | :--- |
| p95 de `ws_connecting` | 144,18 ms (133,74-159,98) | 137,09 ms (123,77-188,26) | < 1 s |
| p99 de broadcast | 15,92 ms (7-25) | 11 ms (5,97-13) | < 100 ms |
| p99 de drift | 0 ms | 0 ms | < 500 ms |
| Sesiones y comprobaciones fallidas | 50 y 0 | 50 y 0 | n/a |

Los rangos se superponen: HSTS no cambia de forma medible el tiempo de apertura de la conexión. Es lo esperado, porque es una cabecera de respuesta y no añade saltos ni handshakes. Una primera tanda del "después" sin calentamiento dio una corrida de 327,22 ms (mediana 153,53 ms); se descartó por comparar sin las mismas condiciones y se repitió con el calentamiento del "antes". Ambas tandas cumplen el umbral de 1 s.

### Excepción: vídeos por MinIO en claro

El gateway ya envía HSTS y cifra todo lo que pasa por Caddy (API, web y WebSockets). MinIO (`:9000`) sigue fuera del gateway porque las URL firmadas SigV4 firman el Host y la ruta ([ADR 0020](adr/0020-entrada-unica-https-y-topologia-produccion.md)), así que la meta de 0 peticiones en claro a los vídeos **no se cumple con este cambio**. Cerrarla exige otra medida: publicar `/tint-media/*` por el gateway sin reescribir la ruta (con una prueba de que la firma sigue siendo válida) o dar TLS propio a MinIO. `docker-compose.production.yml` ya lista el puerto 9000 como excepción hasta entonces.

### Cómo se reprodujo

```bash
# Antes: Caddyfile sin HSTS
git checkout c262cbd -- gateway/Caddyfile
docker compose up -d --no-build --force-recreate gateway
# Después: Caddyfile con HSTS
git checkout HEAD -- gateway/Caddyfile
docker compose up -d --no-build --force-recreate gateway

curl -sk -I https://localhost/healthz                          # cabecera strict-transport-security
curl -s -o /dev/null -w "%{http_code}" http://localhost:9000/minio/health/live
docker run --rm --add-host localhost:<IP-del-host> drwetter/testssl.sh:3.2 \
  --quiet --protocols --headers --ip <IP-del-host> localhost:443
docker run --rm -v "$PWD:/w" -w /w -e SERVER_IP=<IP-del-host> -e BASE_URL=wss://localhost \
  -e JWT_SECRET=<de .env> -e VUS=50 -e DURATION=30s grafana/k6 run \
  --insecure-skip-tls-verify bench/k6/sync-gateway.js
```

`<IP-del-host>` es la IPv4 que ve el contenedor para `host.docker.internal` (`192.168.65.254` en Docker Desktop). El script `sync-gateway.js` resuelve `localhost` con `SERVER_IP` porque el sitio de Caddy es `https://localhost`.

### Limitaciones de esta medición

- Una sola máquina (Windows con Docker Desktop) hace de servidor y de generador de carga; no es la medición de dos máquinas por LAN de la línea base. Por eso se usaron 50 conexiones y no 200, y estos valores solo sirven para comparar el "antes" con el "después" de esta sección, no con los 135 ms de la línea base.
- Con 3 corridas de 30 s, una diferencia de pocos milisegundos queda dentro del ruido (el rango del "después" llega a 188 ms).
- El certificado es interno de Caddy y k6 no lo verifica (`--insecure-skip-tls-verify`); es igual en ambos lados.
- Antes de medir hubo que recrear el stack local con `docker compose up -d --no-build`: `identity-api` seguía en la red `default` anterior a la segmentación (ADR 0027) y el gateway no lo alcanzaba (503 en las rutas autenticadas).
