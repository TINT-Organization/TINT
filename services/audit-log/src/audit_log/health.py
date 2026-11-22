"""`GET /healthz` para el healthcheck de Compose (ADR 0021).

Es la única ruta HTTP: la bitácora no tiene API pública ni ruta en el gateway. Las consultas
se hacen con la CLI dentro del contenedor (`python -m audit_log.list`).
"""

from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8089
# `status` y `service`, como el resto de servicios (ADR 0021).
HEALTH_BODY = b'{"status":"ok","service":"audit-log"}'


class HealthHandler(BaseHTTPRequestHandler):
    server_version = "tint-audit-log"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, HEALTH_BODY)
        else:
            self._send(404, b'{"error":"no encontrado"}')

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s %s", self.address_string(), format % args)


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), HealthHandler)
    server.daemon_threads = True
    return server
