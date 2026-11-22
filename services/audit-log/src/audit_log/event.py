"""Contrato de `audit:events` y su validación.

Lista blanca: solo se guardan los campos del contrato. Cualquier otro campo (correo, IP, nick,
tokens) se descarta sin leerlo (Ley 1581 de 2012).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

STREAM_KEY = "audit:events"
CONSUMER_GROUP = "audit-workers"

OUTCOMES = frozenset({"ok", "denied"})
# `dominio.verbo`, p. ej. `room.delete` o `access.denied`.
ACTION_RE = re.compile(r"^[a-z][a-z_]*\.[a-z][a-z_]*$")
REQUIRED_FIELDS = ("event_id", "action", "outcome", "occurred_at", "source")
OPTIONAL_FIELDS = ("actor_id", "actor_role", "room_id")
MAX_FIELD_LEN = 200


class InvalidEvent(ValueError):
    """El evento no cumple el contrato de `audit:events`."""


def parse_timestamp(value: Any) -> datetime:
    """Convierte un ISO 8601 (`...Z` u offset) a `datetime` en UTC; sin offset se asume UTC."""
    if not isinstance(value, str):
        raise ValueError(f"fecha inválida: {value!r}")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def validate_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Valida el contrato y devuelve solo sus campos, ya normalizados.

    `occurred_at` sale como `datetime` UTC; los opcionales vacíos salen como `None`
    (un `access.denied` sin token no tiene actor).
    """
    missing = [name for name in REQUIRED_FIELDS if not raw.get(name)]
    if missing:
        raise InvalidEvent(f"faltan campos obligatorios: {', '.join(missing)}")

    event: dict[str, Any] = {}
    for name in (*REQUIRED_FIELDS, *OPTIONAL_FIELDS):
        value = raw.get(name)
        if value in (None, ""):
            event[name] = None
            continue
        if not isinstance(value, str):
            raise InvalidEvent(f"{name} debe ser texto")
        # PostgreSQL no admite NUL en text: el evento nunca podría guardarse.
        if "\x00" in value or len(value) > MAX_FIELD_LEN:
            raise InvalidEvent(f"{name} inválido")
        event[name] = value

    try:
        event["event_id"] = str(uuid.UUID(event["event_id"]))
    except ValueError as exc:
        raise InvalidEvent(f"event_id no es un UUID: {event['event_id']!r}") from exc
    if not ACTION_RE.match(event["action"]):
        raise InvalidEvent(f"action inválida: {event['action']!r}")
    if event["outcome"] not in OUTCOMES:
        raise InvalidEvent(f"outcome inválido: {event['outcome']!r}")
    try:
        event["occurred_at"] = parse_timestamp(event["occurred_at"])
    except ValueError as exc:
        raise InvalidEvent(f"occurred_at no es ISO 8601: {event['occurred_at']!r}") from exc
    return event
