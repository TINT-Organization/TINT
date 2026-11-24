# 🤖 AGENT Guidelines — Dominio: Bitácora de auditoría (`services/audit-log`)

## Contexto & Responsabilidad
Registras **quién hizo qué acción sensible, cuándo, en qué sala y con qué resultado** (Prototipo 3, escenario 5: táctica *Audit* de *Recover*, patrón *Security Logger and Auditor*). Consumes `audit:events` de Redis con `XREADGROUP`, guardas cada evento en `audit_entries` (PostgreSQL) y dejas la bitácora **a prueba de cambios**: solo se agregan filas y una cadena de hashes deja ver cualquier edición.

Cómo funciona por dentro y cómo correr las pruebas: [README.md](README.md).

## Stack Técnico
| Pieza | Versión | Por qué |
| :--- | :--- | :--- |
| Python | 3.11 | Igual que analytics y media-worker. |
| `psycopg[binary]` | `==3.2.13` | La misma versión que analytics. |
| `redis` | `==5.2.1` | `XREADGROUP`/`XACK` de `audit:events`; la misma versión que analytics. |
| HTTP | stdlib (`http.server`) | Solo `/healthz`. |
| SQL | Plano y versionado (`src/audit_log/migrations/NNN_*.sql`) | Se aplica al arrancar; la tabla `audit_schema_migrations` registra lo aplicado (`schema_migrations` es de analytics, en la misma base). |
| Pruebas | pytest 8.3.3, pytest-cov 5.0.0, ruff 0.6.9 | Dobles en `tests/fakes.py`; integración con PostgreSQL real si existe `AUDIT_LOG_TEST_DATABASE_URL`. |

## Estructura
| Módulo | Qué hace |
| :--- | :--- |
| `event.py` | Contrato de `audit:events`: lista blanca de campos y validación. |
| `chain.py` | Forma canónica de una fila, `entry_hash` y `verify_rows` (cadena de hashes). |
| `db.py` + `migrations/` | Migraciones, `AuditStore` (escritura encadenada bajo advisory lock), `iter_chain` y `list_entries`. |
| `consumer.py` | `XREADGROUP` → `validate_event` → `AuditStore.save_many` → `XACK`; pendientes y reintentos copiados de analytics. |
| `verify.py`, `list.py` | CLI de solo lectura (`python -m audit_log.verify`, `python -m audit_log.list`). |
| `health.py`, `config.py`, `__main__.py`, `logging_config.py` | `/healthz`, variables de entorno, arranque y apagado, logs JSON. |

## Contrato del stream `audit:events`
Productores: identity-api (acciones sensibles de `src/routes/v1.ts` y accesos negados de `auth.ts`/`requireRoomOwner.ts`). El productor hace `XADD` **sin bloquear la acción** (best-effort, con `MAXLEN` aproximado); si Redis falla, la acción sigue y el hueco se ve en la medición "hechas vs registradas".

Campos planos (texto):

| Campo | Obligatorio | Valor |
| :--- | :--- | :--- |
| `event_id` | sí | UUID; clave de idempotencia (un reintento no duplica la fila). |
| `action` | sí | `dominio.verbo` en minúsculas: `room.create`, `room.update`, `room.delete`, `room.end`, `room.disable`, `invitation.create`, `invitation.revoke`, `guest.revoke`, `ban.create`, `ban.delete`, `access.denied`. |
| `outcome` | sí | `ok` o `denied`. |
| `occurred_at` | sí | ISO 8601 con zona (`…Z`). |
| `source` | sí | Servicio productor (`identity-api`). |
| `actor_id` | no | `sub` del token; vacío si no hubo token (401). |
| `actor_role` | no | `host`, `guest`, … |
| `room_id` | no | Sala afectada, si aplica. |

**Cualquier otro campo se descarta sin guardarse.** Nunca publiques correo, IP, nick, tokens ni texto libre (Ley 1581 de 2012). Un evento que no cumple el contrato se registra como `warning` y se confirma (`XACK`) para no bloquear el stream.

## Modelo de Datos
| Entidad | Almacén | Campos clave |
| :--- | :--- | :--- |
| `audit:events` | Redis Stream (grupo `audit-workers`) | Contrato de arriba. |
| `audit_entries` | PostgreSQL | `seq` (PK, orden de la cadena; puede tener huecos), `event_id` (UUID, único), `action`, `outcome` (`CHECK ok/denied`), `actor_id`, `actor_role`, `room_id`, `source`, `occurred_at`, `recorded_at`, `prev_hash`, `hash`. Índices `(room_id, seq)` y `(actor_id, seq)`. **Sin retención: no se borra.** |
| `audit_schema_migrations` | PostgreSQL | `version` (PK), `applied_at`. |

### Integridad
- **Solo INSERT:** triggers `BEFORE UPDATE OR DELETE` (por fila) y `BEFORE TRUNCATE` lanzan error, también para el dueño de la tabla.
- **Cadena de hashes:** `hash = sha256(prev_hash + "\n" + JSON canónico de la fila)`; la primera fila usa 64 ceros. JSON canónico = los campos de arriba salvo `seq`/`prev_hash`/`hash`, claves ordenadas, fechas en UTC con microsegundos. Un solo escritor a la vez (advisory lock `727102`).
- **Verificación:** `python -m audit_log.verify` sale con 1 y dice el `seq` de la primera fila modificada o borrada. Imprime el último hash (ancla): borrar las últimas filas no rompe la cadena, así que el ancla se guarda fuera de la base para compararla.

## Límites de Dominio
- ❌ Sin API pública, sin puerto publicado y sin ruta en el gateway. Las consultas son por CLI dentro del contenedor.
- ❌ Nunca `UPDATE`/`DELETE` sobre `audit_entries`, ni migraciones que lo hagan. Un cambio de esquema solo agrega columnas o índices.
- ❌ No guardes PII (correo, IP, nick) ni tokens.
- ❌ No bloquees al productor: la bitácora es asíncrona.

## Variables de entorno
| Variable | Defecto | Notas |
| :--- | :--- | :--- |
| `DATABASE_URL` | — (**obligatoria**) | La misma base que identity-api y analytics. |
| `REDIS_URL` | `redis://localhost:6379` | |
| `AUDIT_LOG_PORT` | `8089` | Solo `/healthz`, dentro de la red de Compose. |
| `AUDIT_LOG_CONSUMER` | hostname | Nombre estable por réplica: al reiniciar recupera sus pendientes. |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warn`, `error`. |

## Gate
`ruff check . && ruff format --check . && pytest --cov=src --cov-fail-under=85`
