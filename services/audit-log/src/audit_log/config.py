"""Configuración desde variables de entorno (sin secretos por defecto: falla cerrado)."""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field

from audit_log.health import DEFAULT_PORT


class ConfigError(Exception):
    """Faltan variables obligatorias o tienen un valor inválido."""


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    redis_url: str = "redis://localhost:6379"
    port: int = DEFAULT_PORT
    consumer: str = field(default_factory=socket.gethostname)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        if not env.get("DATABASE_URL"):
            raise ConfigError("faltan variables de entorno: DATABASE_URL")

        optional: dict = {}
        if env.get("REDIS_URL"):
            optional["redis_url"] = env["REDIS_URL"]
        # Nombre estable por réplica: al reiniciar recupera sus propios pendientes.
        if env.get("AUDIT_LOG_CONSUMER"):
            optional["consumer"] = env["AUDIT_LOG_CONSUMER"]
        if env.get("AUDIT_LOG_PORT"):
            port = env["AUDIT_LOG_PORT"]
            if not port.isdigit() or not 0 < int(port) < 65536:
                raise ConfigError(f"AUDIT_LOG_PORT inválido: {port!r}")
            optional["port"] = int(port)
        return cls(database_url=env["DATABASE_URL"], **optional)
