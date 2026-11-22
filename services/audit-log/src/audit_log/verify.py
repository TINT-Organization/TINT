"""`python -m audit_log.verify`: revisa la cadena de hashes de toda la bitácora.

Código de salida: 0 si la cadena está entera, 1 si está rota (imprime la primera fila que no
cuadra), 2 si falta configuración o no hay conexión. Solo lee; se corre dentro del contenedor
(`docker compose exec audit-log python -m audit_log.verify`).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

import psycopg

from audit_log.chain import ChainBreak, verify_rows
from audit_log.db import iter_chain


def main(env: Mapping[str, str] | None = None, connect=psycopg.connect, out=None) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    if not env.get("DATABASE_URL"):
        print("audit-log verify: falta DATABASE_URL", file=sys.stderr)
        return 2
    try:
        with connect(env["DATABASE_URL"], autocommit=True) as conn:
            count, last_seq, last_hash = verify_rows(iter_chain(conn))
    except ChainBreak as exc:
        print(f"ROTA: {exc}", file=out)
        return 1
    except psycopg.Error as exc:
        print(f"audit-log verify: no se pudo leer la bitácora: {exc}", file=sys.stderr)
        return 2
    print(f"OK: {count} filas, cadena entera (último seq={last_seq}, ancla={last_hash})", file=out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
