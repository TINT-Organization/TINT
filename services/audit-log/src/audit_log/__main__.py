"""Punto de entrada (`python -m audit_log`).

1. Aplica las migraciones pendientes (`migrations/*.sql`).
2. Arranca en hilos el consumidor de `audit:events` y `/healthz`.
3. Con SIGTERM/SIGINT detiene `/healthz`, espera al consumidor y cierra la conexión.
"""

from __future__ import annotations

import functools
import logging
import os
import signal
import sys
import threading
from collections.abc import Mapping

import psycopg
import redis

from audit_log.config import ConfigError, Settings
from audit_log.consumer import StreamConsumer
from audit_log.db import AuditStore, apply_migrations
from audit_log.event import STREAM_KEY
from audit_log.health import create_server
from audit_log.logging_config import LogLevelError, install_logging

logger = logging.getLogger("audit_log")

SHUTDOWN_TIMEOUT_S = 10.0


def install_signal_handlers(stop: threading.Event) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())


def main(
    env: Mapping[str, str] | None = None,
    stop: threading.Event | None = None,
    connect=psycopg.connect,
    redis_factory=redis.Redis.from_url,
    server_factory=create_server,
) -> int:
    env = os.environ if env is None else env
    try:
        install_logging("audit-log", env)
    except LogLevelError as exc:
        print(f"audit-log: {exc}", file=sys.stderr)
        return 2
    try:
        settings = Settings.from_env(env)
    except ConfigError as exc:
        print(f"audit-log: {exc}", file=sys.stderr)
        return 2

    connect_db = functools.partial(connect, settings.database_url, autocommit=True)
    try:
        with connect_db() as conn:
            applied = apply_migrations(conn)
    except psycopg.Error:
        logger.exception("no se pudieron aplicar las migraciones")
        return 1
    logger.info("migraciones aplicadas: %s", ", ".join(applied) or "ninguna pendiente")

    if stop is None:
        stop = threading.Event()
        install_signal_handlers(stop)

    store = AuditStore(connect_db)
    consumer = StreamConsumer(
        redis_factory(settings.redis_url, decode_responses=True), store, settings.consumer
    )
    server = server_factory("0.0.0.0", settings.port)
    worker = threading.Thread(target=consumer.run, args=(stop,), name="consumer")
    health = threading.Thread(target=server.serve_forever, name="health", daemon=True)
    worker.start()
    health.start()
    logger.info("audit-log consumiendo %s; /healthz en :%d", STREAM_KEY, settings.port)

    stop.wait()
    logger.info("apagando audit-log")
    server.shutdown()
    server.server_close()
    worker.join(SHUTDOWN_TIMEOUT_S)
    store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
