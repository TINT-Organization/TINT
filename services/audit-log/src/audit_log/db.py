"""PostgreSQL de la bitácora: migraciones, escritura encadenada y lecturas de la CLI.

Las conexiones se piden a una fábrica (`connect`) en modo autocommit; cada lote leído del stream
va en una transacción explícita, así el consumidor solo hace `XACK` cuando sus filas ya están
confirmadas en la base.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import psycopg

from audit_log.chain import GENESIS, entry_hash
from audit_log.event import InvalidEvent

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
# Claves fijas de advisory lock (distintas de la de analytics, 727_001: comparten la base).
# Migraciones: dos réplicas no las aplican a la vez.
MIGRATIONS_LOCK_ID = 727_101
# Cadena: un solo escritor a la vez lee el último hash y agrega filas.
CHAIN_LOCK_ID = 727_102
# La tabla `schema_migrations` es de analytics; la bitácora lleva la suya.
CREATE_MIGRATIONS_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS audit_schema_migrations "
    "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
)
SELECT_APPLIED_SQL = "SELECT version FROM audit_schema_migrations"
INSERT_APPLIED_SQL = "INSERT INTO audit_schema_migrations (version) VALUES (%s)"

ENTRY_COLUMNS = (
    "event_id",
    "action",
    "outcome",
    "actor_id",
    "actor_role",
    "room_id",
    "source",
    "occurred_at",
    "recorded_at",
    "prev_hash",
    "hash",
)
ROW_COLUMNS = ("seq", *ENTRY_COLUMNS)
CHAIN_LOCK_SQL = "SELECT pg_advisory_xact_lock(%s)"
LAST_HASH_SQL = "SELECT hash FROM audit_entries ORDER BY seq DESC LIMIT 1"
EXISTS_SQL = "SELECT 1 FROM audit_entries WHERE event_id = %s"
INSERT_ENTRY_SQL = (
    f"INSERT INTO audit_entries ({', '.join(ENTRY_COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(ENTRY_COLUMNS))})"
)
# `event_id::text` para que la fila leída sea igual a la que se hasheó (UUID → texto).
SELECT_COLUMNS = ", ".join("event_id::text" if c == "event_id" else c for c in ROW_COLUMNS)
SELECT_CHAIN_PAGE_SQL = (
    f"SELECT {SELECT_COLUMNS} FROM audit_entries WHERE seq > %s ORDER BY seq LIMIT %s"
)
CHAIN_PAGE_SIZE = 1000

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class Migration(NamedTuple):
    version: str
    sql: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Archivos `NNN_nombre.sql` del directorio, en orden de versión."""
    return [Migration(path.stem, path.read_text()) for path in sorted(directory.glob("*.sql"))]


def apply_migrations(conn, migrations: Iterable[Migration] | None = None) -> list[str]:
    """Aplica en una transacción las migraciones pendientes y devuelve sus versiones."""
    migrations = load_migrations() if migrations is None else list(migrations)
    applied_now = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATIONS_LOCK_ID,))
        conn.execute(CREATE_MIGRATIONS_TABLE_SQL)
        done = {row[0] for row in conn.execute(SELECT_APPLIED_SQL).fetchall()}
        for migration in migrations:
            if migration.version in done:
                continue
            conn.execute(migration.sql)
            conn.execute(INSERT_APPLIED_SQL, (migration.version,))
            applied_now.append(migration.version)
    return applied_now


class AuditStore:
    """Escritura del consumidor: una conexión larga que se reabre si se pierde."""

    def __init__(self, connect: Callable[[], Any], clock: Clock = utc_now) -> None:
        self._connect = connect
        self._clock = clock
        self._conn = None

    def _connection(self):
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def save(self, event: dict[str, Any]) -> bool:
        """Guarda un evento validado (idempotente por `event_id`); True si era nuevo."""
        return self.save_many([event]) == 1

    def save_many(self, events: Iterable[dict[str, Any]]) -> int:
        """Agrega un lote de eventos validados a la cadena en una transacción.

        Toma el lock de la cadena, lee el último hash y encadena cada evento nuevo; los que ya
        estaban (mismo `event_id`) se saltan. Devuelve cuántos agregó. Un dato que PostgreSQL
        rechaza deshace el lote y se reporta como `InvalidEvent` (reintentarlo no serviría):
        quien llama lo reintenta evento por evento. Cualquier otro error descarta la conexión y
        se propaga para reintentar.
        """
        inserted = 0
        try:
            conn = self._connection()
            with conn.transaction():
                conn.execute(CHAIN_LOCK_SQL, (CHAIN_LOCK_ID,))
                last = conn.execute(LAST_HASH_SQL).fetchone()
                prev_hash = GENESIS if last is None else last[0]
                recorded_at = self._clock()
                seen = set()
                for event in events:
                    if event["event_id"] in seen:
                        continue
                    seen.add(event["event_id"])
                    if conn.execute(EXISTS_SQL, (event["event_id"],)).fetchone():
                        continue
                    entry = {**event, "recorded_at": recorded_at, "prev_hash": prev_hash}
                    entry["hash"] = entry_hash(prev_hash, entry)
                    conn.execute(INSERT_ENTRY_SQL, tuple(entry[c] for c in ENTRY_COLUMNS))
                    prev_hash = entry["hash"]
                    inserted += 1
        except (psycopg.DataError, psycopg.IntegrityError) as exc:
            raise InvalidEvent(f"PostgreSQL rechazó el evento: {exc}") from exc
        except Exception:
            self.close()
            raise
        return inserted

    def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            try:
                conn.close()
            except Exception:  # pragma: no cover - cerrar una conexión rota no debe fallar
                logger.debug("error cerrando la conexión", exc_info=True)


def iter_chain(conn, page_size: int = CHAIN_PAGE_SIZE):
    """Filas de la bitácora en orden de `seq`, por páginas (no carga toda la tabla)."""
    last_seq = 0
    while True:
        rows = conn.execute(SELECT_CHAIN_PAGE_SQL, (last_seq, page_size)).fetchall()
        for row in rows:
            yield dict(zip(ROW_COLUMNS, row, strict=True))
        if len(rows) < page_size:
            return
        last_seq = rows[-1][0]


def list_entries(
    conn, room_id: str | None = None, actor_id: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Las `limit` filas más recientes, filtradas por sala y/o actor, de la más vieja a la nueva."""
    where, params = [], []
    if room_id:
        where.append("room_id = %s")
        params.append(room_id)
    if actor_id:
        where.append("actor_id = %s")
        params.append(actor_id)
    sql = f"SELECT {SELECT_COLUMNS} FROM audit_entries"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY seq DESC LIMIT %s"
    rows = conn.execute(sql, (*params, limit)).fetchall()
    return [dict(zip(ROW_COLUMNS, row, strict=True)) for row in reversed(rows)]
