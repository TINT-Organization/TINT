"""`python -m audit_log.list [--room ID] [--actor ID] [--limit N]`: consulta la bitácora.

Imprime una fila JSON por línea, de la más vieja a la más nueva. Solo lee; se corre dentro del
contenedor (`docker compose exec audit-log python -m audit_log.list --room <id>`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime

import psycopg

from audit_log.db import list_entries


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m audit_log.list")
    parser.add_argument("--room", help="solo las acciones de esta sala")
    parser.add_argument("--actor", help="solo las acciones de este usuario (sub)")
    parser.add_argument("--limit", type=int, default=100, help="máximo de filas (100)")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit debe ser mayor que 0")
    return args


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)  # pragma: no cover


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    connect=psycopg.connect,
    out=None,
) -> int:
    args = _parse_args(argv)
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    if not env.get("DATABASE_URL"):
        print("audit-log list: falta DATABASE_URL", file=sys.stderr)
        return 2
    try:
        with connect(env["DATABASE_URL"], autocommit=True) as conn:
            rows = list_entries(conn, room_id=args.room, actor_id=args.actor, limit=args.limit)
    except psycopg.Error as exc:
        print(f"audit-log list: no se pudo leer la bitácora: {exc}", file=sys.stderr)
        return 2
    for row in rows:
        print(json.dumps(row, default=_json_default, ensure_ascii=False), file=out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
