"""Cadena de hashes de la bitácora: cada fila guarda el hash de la anterior.

`hash = sha256(prev_hash + "\\n" + forma canónica de la fila)`. La primera fila usa `GENESIS`
como `prev_hash`. Cambiar, borrar o reordenar una fila rompe la cadena desde ese punto, y
`python -m audit_log.verify` lo detecta. La forma canónica es JSON con claves ordenadas y
fechas en UTC con microsegundos (la precisión de `timestamptz`), así que se reconstruye igual
al leer la fila de PostgreSQL.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

GENESIS = "0" * 64
# Campos que entran al hash: todo lo que se guarda salvo `seq`, `prev_hash` y `hash`.
HASHED_FIELDS = (
    "event_id",
    "action",
    "outcome",
    "actor_id",
    "actor_role",
    "room_id",
    "source",
    "occurred_at",
    "recorded_at",
)


def _canonical_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    return None if value is None else str(value)


def canonical(entry: dict[str, Any]) -> str:
    """Forma canónica de la fila (estable entre Python y lo que devuelve PostgreSQL)."""
    data = {name: _canonical_value(entry.get(name)) for name in HASHED_FIELDS}
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(prev_hash: str, entry: dict[str, Any]) -> str:
    return hashlib.sha256(f"{prev_hash}\n{canonical(entry)}".encode()).hexdigest()


class ChainBreak(Exception):
    """La cadena no cuadra en la fila `seq`."""

    def __init__(self, seq: int, reason: str) -> None:
        super().__init__(f"cadena rota en seq={seq}: {reason}")
        self.seq = seq
        self.reason = reason


def verify_rows(rows) -> tuple[int, int | None, str]:
    """Recorre filas en orden de `seq` y lanza `ChainBreak` en la primera que no cuadra.

    Cada fila es un dict con `seq`, `prev_hash`, `hash` y los `HASHED_FIELDS`. Devuelve
    `(filas revisadas, último seq, último hash)`; el último hash sirve de ancla externa (borrar
    las filas del final no rompe la cadena, pero sí cambia el ancla).
    """
    expected_prev = GENESIS
    count = 0
    last_seq = None
    for row in rows:
        seq = row["seq"]
        if row["prev_hash"] != expected_prev:
            raise ChainBreak(seq, "prev_hash no es el hash de la fila anterior (fila borrada?)")
        if entry_hash(row["prev_hash"], row) != row["hash"]:
            raise ChainBreak(seq, "el hash no coincide con el contenido (fila modificada?)")
        expected_prev = row["hash"]
        last_seq = seq
        count += 1
    return count, last_seq, expected_prev
