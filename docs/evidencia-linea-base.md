# Evidencia de la línea base ("antes") del Prototipo 3

Registro de qué prueba se aplicó, con qué herramienta y qué resultado dio, sobre `develop` sin ninguna medida de seguridad. El método está en [pruebas-seguridad.md](pruebas-seguridad.md); los escenarios y las medidas a comparar, en el documento del Prototipo 3.

| Campo | Valor |
| :--- | :--- |
| Fecha de las mediciones | 2026-11-16 (calibración y corridas 12:3x-12:51, comprobaciones de seguridad 12:53, hora de Colombia) |
| Código medido | `develop`, commit `5767874` (imágenes ya construidas de ese commit) |
| Servidor | Windows con Docker Desktop 29.8.2, 12 CPU y ~7,7 GB de RAM para Docker; stack aislado `tintbench` en modo Firebase Auth Emulator, datos sintéticos |
| Generador de carga | portátil por ethernet en la misma LAN (~1 ms), k6 v2.4.0; la carga entra por el gateway del repo (`tint-gateway`, puerto 8443) |
| Certificado | interno de Caddy; k6 con `--insecure-skip-tls-verify` y `localhost` resuelto a la IP del servidor |
| Corridas | 3 por prueba de carga, cada una con el stack recreado desde cero (`down -v` y `up`) y 30 s de calentamiento sin medir |
| Ruido | `transcriptor` (otro compose del servidor) en reposo (0,11 % de CPU) antes de cada corrida |

## 1. Pruebas aplicadas y resultado

| # | Escenario | Prueba | Herramienta | Resultado en `develop` |
| :--- | :--- | :--- | :--- | :--- |
| P1 | Global | Capacidad sostenida de la API (calibración por escalones) | k6, `bench/k6/api.js` | **400 req/s**; 800 req/s satura (ver 2.1) |
| P2 | Global | Latencia p50, p95 y p99 de la API a 200 req/s (50 % de la capacidad) | k6, `bench/k6/api.js` | p50 9,70 ms, p95 13,75 ms, p99 15,40 ms, 0 fallos |
| P3 | Global | Peticiones por segundo sostenidas | k6 | 199 req/s a la tasa pedida de 200 (sin iteraciones perdidas) |
| P4 | 2 | Ráfaga de 300 req/s a un endpoint autenticado durante 15 s | k6, `PHASE=burst` | 4500 respuestas 2xx, **0 respuestas 429** (no hay límite de tasa) |
| P5 | Global | Canal Sync por el gateway: p99 de broadcast, p99 de drift, p95 de `ws_connecting` | k6, `sync.js` (sync-core) vía `bench/k6/sync-gateway.js`, 200 conexiones, 60 s | 14 ms, 0 ms y 135 ms; umbrales del repo cumplidos |
| P6 | Global | CPU y memoria del gateway, identity-api, postgres, redis y sync-core durante cada fase | `docker stats` por SSH | ver 2.5 |
| P7 | 1 | Versiones de TLS ofrecidas por el gateway | `testssl.sh` 3.2.3 | solo TLS 1.2 y TLS 1.3; ningún protocolo anterior |
| P8 | 1 | Cabecera HSTS presente | `curl -I` | **ausente** |
| P9 | 1 | Peticiones en claro a medios | `curl http://<ip>:9000/minio/health/live` | **accesible por HTTP en claro** |
| P10 | 2 | Ruta interna `POST /api/v1/internal/rooms/x/disable` sin token | `curl` | **401** (no 404) |
| P11 | 2 | La misma ruta con un token de host válido | `curl` | 404 |
| P12 | 2 | Cabecera `Server` expuesta | `curl -I` | **sí** |
| P13 | 3 | Puertos de bases de datos alcanzables desde la LAN | `nmap -p-` y conexión TCP directa | **PostgreSQL 5432, Redis 6379, MongoDB 27017 y MinIO 9000/9001 abiertos** (4 almacenes) |
| P14 | 3 | Puertos publicados | `nmap -p-` | servicios de TINT en 5173 y 8080-8086, gateway en 8443, más los del sistema operativo del servidor |

## 2. Detalle

### 2.1 Calibración de capacidad (P1)

Escalones de 30 s con tasa de llegada constante (`constant-arrival-rate`). Un escalón cumple si p95 ≤ 500 ms, fallos ≤ 1 % y se pierden ≤ 1 % de iteraciones.

| Tasa pedida (req/s) | p95 | Fallos | Iteraciones perdidas | Cumple |
| ---: | ---: | ---: | ---: | :---: |
| 25 | 14,4 ms | 0 % | 0 % | sí |
| 50 | 13,4 ms | 0 % | 0 % | sí |
| 100 | 11,8 ms | 0 % | 0 % | sí |
| 200 | 14,0 ms | 0 % | 0 % | sí |
| 400 | 25,6 ms | 0 % | 0 % | sí |
| 800 | 6312 ms | 16,7 % | 42,6 % | no |

Capacidad sostenida: 400 req/s. La fase de latencia (P2) usa 200 req/s. El "después" debe correr con `--rate 200` para comparar a la misma carga.

### 2.2 Latencia de la API a 200 req/s (P2, P3)

Mezcla ponderada: 10 % `/healthz`, 30 % `GET /rooms`, 25 % `POST /rooms/:id/sync-ticket`, 20 % `GET /rooms/:id/playback`, 15 % `POST /invitations/claim`. Duración 120 s. Todas las comprobaciones de respuesta 2xx pasaron en las 3 corridas (24 001, 24 000 y 24 001 comprobaciones, 0 fallidas), lo que confirma que los tokens de k6 atraviesan el `forward_auth` del gateway.

| Corrida | p50 | p95 | p99 | Tasa | Fallos |
| :--- | ---: | ---: | ---: | ---: | ---: |
| 1 | 9,70 ms | 13,74 ms | 15,38 ms | 199,03 req/s | 0 % |
| 2 | 9,69 ms | 13,75 ms | 15,40 ms | 199,04 req/s | 0 % |
| 3 | 9,73 ms | 13,95 ms | 16,10 ms | 199,07 req/s | 0 % |
| **Mediana (mín-máx)** | 9,70 (9,69-9,73) | 13,75 (13,74-13,95) | 15,40 (15,38-16,10) | 199,04 | 0 % |

### 2.3 Ráfaga a 300 req/s (P4)

`GET /api/v1/rooms` autenticado, 300 req/s durante 15 s. Quedó por debajo de la capacidad calibrada (400 req/s), así que no hay saturación.

| Corrida | Respuestas 2xx | Respuestas 429 | p95 |
| :--- | ---: | ---: | ---: |
| 1 | 4500 | 0 | 19,54 ms |
| 2 | 4501 | 0 | 21,24 ms |
| 3 | 4500 | 0 | 20,89 ms |

Sin rate limit, el 100 % de la ráfaga se atiende. Para el "después", el valor a comparar es la fracción de respuestas 429 con la misma ráfaga.

### 2.4 Canal Sync por el gateway (P5)

200 conexiones WebSocket a una sala durante 60 s, con tokens HS256 firmados con el secreto sintético del banco; el `forward_auth` valida el token antes del upgrade.

| Corrida | p99 broadcast (umbral < 100 ms) | p99 drift (umbral < 500 ms) | p95 `ws_connecting` (umbral < 1 s) | Sesiones | `FORBIDDEN` a participantes |
| :--- | ---: | ---: | ---: | ---: | ---: |
| 1 | 14 ms | 0 ms | 169,21 ms | 200 | 100 % |
| 2 | 23 ms | 0 ms | 125,76 ms | 200 | 100 % |
| 3 | 12 ms | 0 ms | 135,19 ms | 200 | 100 % |
| **Mediana (mín-máx)** | 14 (12-23) | 0 | 135,19 (125,76-169,21) | 200 | 100 % |

Código de salida de k6 0 en las 3 corridas y en las 3 fases: todos los umbrales cumplidos.

### 2.5 Recursos (P6)

CPU media / máxima y memoria máxima, mediana de las 3 corridas.

| Contenedor | API a 200 req/s | Ráfaga a 300 req/s | Sync, 200 conexiones |
| :--- | :--- | :--- | :--- |
| gateway | 25,4 % / 31,3 %, 28 MiB | 32,5 % / 48,2 %, 29 MiB | 3,5 % / 49,5 %, 55 MiB |
| identity-api | 45,0 % / 60,8 %, 99 MiB | 58,4 % / 97,0 %, 117 MiB | 1,8 % / 19,4 %, 117 MiB |
| postgres | 19,2 % / 24,9 %, 64 MiB | 31,1 % / 46,2 %, 67 MiB | 0,7 % / 3,7 %, 65 MiB |
| redis | 2,5 % / 4,9 %, 9 MiB | 0,9 % / 3,5 %, 6 MiB | 1,1 % / 7,0 %, 8 MiB |
| sync-core | 0,3 % / 2,6 %, 4 MiB | 0,3 % / 2,7 %, 4 MiB | 3,0 % / 26,9 %, 27 MiB |

### 2.6 Superficie de seguridad (P7 a P14)

Corridas desde el portátil (fuera de la máquina del servidor), `bench/security_checks.py`, 12:53.

| Comprobación | Resultado |
| :--- | :--- |
| Protocolos TLS ofrecidos (`testssl.sh`) | TLS 1.2 y TLS 1.3; SSLv2, SSLv3, TLS 1.0 y TLS 1.1 no se ofrecen |
| `Strict-Transport-Security` | ausente |
| `Server: Caddy` en las respuestas | expuesto |
| `POST /api/v1/internal/rooms/x/disable` sin token | 401 |
| Misma petición con token de host | 404 |
| MinIO (`:9000`) por HTTP sin cifrar | responde |
| Conexión TCP directa a PostgreSQL, Redis, MongoDB y consola de MinIO | las 4 conectan |
| Puertos abiertos de TINT | 5173, 5432, 6379, 8080-8086, 8443, 9000, 9001, 27017 |

Además aparecen abiertos puertos propios del sistema operativo del servidor (SSH, RPC de Windows y otros) que no son de TINT.

## 3. Lectura de los resultados

- La línea base es estable: la variación entre corridas es de ~0,2 ms en el p95 de la API y de algunos ms en Sync. Una medida de seguridad solo es concluyente si cambia una métrica por más que ese rango (`bench/summarize.py` lo marca).
- El escenario 1 ya cumple "TLS 1.2 o superior" en `develop`. Lo que falta medir como mejora es HSTS (P8) y MinIO en claro (P9); el resultado del "después" está en [pruebas-seguridad.md](pruebas-seguridad.md#resultados-escenario-1-canal-seguro-antes-y-después-de-hsts).
- El escenario 2 parte de 0 respuestas 429 (P4), de 401 sin token en la ruta interna (P10) y del header `Server` expuesto (P12).
- El escenario 3 parte de 4 almacenes alcanzables desde la LAN (P13).
- Los escenarios 4 y 5 no tienen prueba en esta línea base: dependen de medidas que aún no existen (audiencia por servicio y bitácora) y se medirán con el "después".

## 4. Limitaciones

- No se registró la CPU del portátil durante las corridas. El escalón de 800 req/s pudo estar limitado por el generador y no por el servidor; la capacidad de 400 req/s es un mínimo fiable para comparar a 200 req/s, no un máximo del sistema.
- Con `GATEWAY_HOST=localhost` y el certificado interno de Caddy, k6 no verifica el certificado. Es igual en el "antes" y en el "después".
- Los resultados crudos (JSON de k6, CSV de `docker stats` y registros) quedan en `bench/results/antes-develop/`, que no se versiona. Este documento reproduce los valores que sustentan las conclusiones.

## 5. Cómo se reprodujo

```bash
python3 bench/run.py prepare
python3 bench/run.py measure --label antes-develop
python3 bench/run.py security --label antes-develop
python3 bench/run.py restore
python3 bench/summarize.py bench/results/antes-develop
```
