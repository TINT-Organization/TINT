#!/usr/bin/env python3
"""Escenario 4 (ADR 0030): matriz de tokens cruzados por la entrada real, sin carga.

Para cada ticket que un cliente puede obtener en la etapa medida, lo presenta en cada entrada de
servicio a través del gateway y anota si se aceptó o se rechazó:

  entradas   /ws/sync · /ws/social · /ws/voice (handshake con ?room_id&token) · WHEP
             POST /api/v1/stream/rooms/{id}/whep · analytics GET /api/v1/rooms/{id}/kpis
  tickets    E0–E3: sync-ticket y analytics-ticket (aud tint:room) · E4: POST /tickets/{sync,social,
             voice,stream,analytics} · además, en toda etapa, la sesión del invitado y un token
             `tint:room` firmado con el JWT_SECRET del banco (cliente viejo; AC5)

Además mide el p95 de emitir un ticket (AC6) con varios hosts en ronda (el límite por usuario de
Caddy es de 100 peticiones cada 10 s).

  python3 bench/s4_matrix.py --port 8443 --env-file bench/results/_src/e4/.env --out bench/results/s4-e4.json

Solo biblioteca estándar. La lógica pura (clasificación, marcos WebSocket, porcentajes) la prueban
gateway/tests/test_s4_matrix.py.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import socket
import ssl
import statistics
import struct
import sys
import time
import uuid
from pathlib import Path

ENTRIES = ["sync", "social", "voice", "stream", "analytics"]
SERVICE_TICKETS = ["sync", "social", "voice", "stream", "analytics"]
# Servicio "dueño" de cada ticket: presentarlo en otra entrada es un token cruzado (AC1).
OWNER = {
    "ticket:sync": "sync", "ticket:social": "social", "ticket:voice": "voice", "ticket:stream": "stream",
    "ticket:analytics": "analytics", "sync-ticket": "sync", "analytics-ticket": "analytics",
}
# Cierres tras el upgrade que significan "token no aceptado" (ADR 0007/0014: revocado, caducado o inválido).
AUTH_CLOSE_CODES = {4001, 4003, 1008}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def emulator_token(sub: str, project: str = "demo-tint") -> str:
    """ID token del Firebase Auth Emulator (alg none): solo lo acepta el stack de pruebas (ADR 0024)."""
    now = int(time.time())
    claims = {"iss": f"https://securetoken.google.com/{project}", "aud": project, "sub": sub, "iat": now,
              "exp": now + 3600, "email": f"{sub}@bench.invalid", "name": f"Host {sub}"}
    return f"{b64url(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode())}.{b64url(json.dumps(claims).encode())}."


def sign_hs256(claims: dict, secret: str) -> str:
    head = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64url(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{b64url(sig)}"


def jwt_claims(token: str) -> dict:
    """Claims sin verificar (solo para anotar la audiencia en el informe)."""
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


def parse_frames(buf: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    """Marcos WebSocket completos del servidor (sin máscara): ([(opcode, payload)], resto sin completar)."""
    frames = []
    while len(buf) >= 2:
        length, offset = buf[1] & 0x7F, 2
        if length == 126:
            if len(buf) < 4:
                break
            length, offset = struct.unpack(">H", buf[2:4])[0], 4
        elif length == 127:
            if len(buf) < 10:
                break
            length, offset = struct.unpack(">Q", buf[2:10])[0], 10
        if len(buf) < offset + length:
            break
        frames.append((buf[0] & 0x0F, buf[offset:offset + length]))
        buf = buf[offset + length:]
    return frames, buf


def close_code(payload: bytes) -> int:
    """Código de un marco de cierre (1005 si viene sin código)."""
    return struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1005


def classify_http(status: int) -> str:
    """REST: 401/403 = rechazado por autenticación o autorización; 0 = sin respuesta."""
    if status in (401, 403):
        return "rechazado"
    if status == 0 or status >= 500 or status == 429:
        return "error"
    return "aceptado"


def classify_ws(status: int, close_code: int | None) -> str:
    """WebSocket: 101 abierto = aceptado; 401/403 o cierre 4001/4003/1008 = rechazado; otro cierre = cerrado."""
    if status == 101:
        if close_code is None:
            return "aceptado"
        return "rechazado" if close_code in AUTH_CLOSE_CODES else "cerrado"
    return classify_http(status)


def crossed_summary(matrix: dict[str, dict[str, dict]]) -> dict:
    """% de tokens cruzados (ticket de un servicio en otro) rechazados, y celdas propias aceptadas."""
    crossed = own = crossed_rejected = own_accepted = 0
    for ticket, row in matrix.items():
        owner = OWNER.get(ticket)
        if owner is None:
            continue
        for entry, cell in row.items():
            if entry == owner:
                own += 1
                own_accepted += cell["resultado"] == "aceptado"
            else:
                crossed += 1
                crossed_rejected += cell["resultado"] == "rechazado"
    return {
        "cruzados": crossed,
        "cruzados_rechazados": crossed_rejected,
        "pct_cruzados_rechazados": round(100.0 * crossed_rejected / crossed, 1) if crossed else None,
        "propios": own,
        "propios_aceptados": own_accepted,
    }


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100.0
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


# --- E/S -----------------------------------------------------------------------------------------


class Gateway:
    """Cliente HTTPS mínimo contra https://localhost:<port> (certificado propio del stack: sin verificar)."""

    def __init__(self, ip: str, port: int):
        self.ip, self.port = ip, port
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        self.conn: http.client.HTTPSConnection | None = None

    def request(self, method: str, path: str, token: str = "", body: bytes | None = None,
                content_type: str = "application/json") -> tuple[int, bytes]:
        headers = {"Host": f"localhost:{self.port}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = content_type
        for attempt in range(2):
            if self.conn is None:
                self.conn = http.client.HTTPSConnection(self.ip, self.port, context=self.ctx, timeout=10)
            try:
                self.conn.request(method, path, body=body, headers=headers)
                resp = self.conn.getresponse()
                return resp.status, resp.read()
            except (OSError, http.client.HTTPException):
                self.conn.close()
                self.conn = None
                if attempt:
                    return 0, b""
        return 0, b""

    def json(self, method: str, path: str, token: str = "", payload: dict | None = None) -> tuple[int, dict]:
        body = json.dumps(payload).encode() if payload is not None else None
        status, raw = self.request(method, path, token, body)
        try:
            return status, json.loads(raw or b"{}")
        except ValueError:
            return status, {}

    def websocket(self, path: str, wait_s: float = 2.0) -> tuple[int, int | None]:
        """Handshake WebSocket por TLS; devuelve (estado HTTP, código de cierre si llega en `wait_s`)."""
        key = base64.b64encode(os.urandom(16)).decode()
        raw = socket.create_connection((self.ip, self.port), timeout=10)
        sock = self.ctx.wrap_socket(raw, server_hostname="localhost")
        try:
            sock.sendall((f"GET {path} HTTP/1.1\r\nHost: localhost:{self.port}\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            try:
                status = int(head.split(b" ", 2)[1])
            except (IndexError, ValueError):
                return 0, None
            if status != 101:
                return status, None
            # Lee marcos hasta un cierre o hasta agotar la espera (un canal sano manda estado y se queda abierto).
            sock.settimeout(0.3)
            deadline = time.time() + wait_s
            while time.time() < deadline:
                frames, rest = parse_frames(rest)
                for opcode, payload in frames:
                    if opcode == 0x8:
                        return 101, close_code(payload)
                try:
                    chunk = sock.recv(65536)
                except (TimeoutError, socket.timeout):
                    continue
                if not chunk:
                    return 101, 1006
                rest += chunk
            return 101, None
        finally:
            sock.close()


WHEP_OFFER = (b"v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=group:BUNDLE 0\r\n"
              b"m=video 9 UDP/TLS/RTP/SAVPF 96\r\nc=IN IP4 0.0.0.0\r\na=mid:0\r\na=recvonly\r\n"
              b"a=ice-ufrag:bnch\r\na=ice-pwd:benchbenchbenchbenchbench\r\n"
              b"a=fingerprint:sha-256 00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00\r\n"
              b"a=setup:actpass\r\na=rtcp-mux\r\na=rtpmap:96 VP8/90000\r\n")


def present(gw: Gateway, token: str, entry: str, room_id: str) -> dict:
    """Presenta `token` en la entrada `entry` y clasifica la respuesta."""
    if entry in ("sync", "social", "voice"):
        status, close = gw.websocket(f"/ws/{entry}?room_id={room_id}&token={token}")
        return {"http": status, "cierre": close, "resultado": classify_ws(status, close)}
    if entry == "stream":
        status, raw = gw.request("POST", f"/api/v1/stream/rooms/{room_id}/whep", token, WHEP_OFFER, "application/sdp")
    else:
        status, raw = gw.request("GET", f"/api/v1/rooms/{room_id}/kpis", token)
    return {"http": status, "cuerpo": raw[:80].decode(errors="replace"), "resultado": classify_http(status)}


def new_host(gw: Gateway, label: str) -> tuple[str, str, str]:
    """Crea un host (token del emulador), una sala y reclama su invitación: (token host, sala, token invitado)."""
    token = emulator_token(f"s4-{label}-{uuid.uuid4().hex[:8]}")
    status, created = gw.json("POST", "/api/v1/rooms", token, {"title": f"S4 {label}"})
    if status != 200:
        raise RuntimeError(f"crear sala: {status} {created}")
    room_id, code = created["room"]["id"], created["invitation"]["code"]
    status, claim = gw.json("POST", "/api/v1/invitations/claim", "", {"code": code, "nick": f"inv-{label}"})
    if status != 200:
        raise RuntimeError(f"reclamar invitación: {status} {claim}")
    return token, room_id, claim["token"]


def obtain_tickets(gw: Gateway, host: str, guest: str, room_id: str, secret: str, issuer: str) -> tuple[dict, dict]:
    """Todos los tokens que un cliente puede obtener en la etapa (y cómo respondió cada endpoint)."""
    tokens, issued = {}, {}
    for name, path in (("sync-ticket", "sync-ticket"), ("analytics-ticket", "analytics-ticket")):
        status, body = gw.json("POST", f"/api/v1/rooms/{room_id}/{path}", host)
        issued[name] = status
        if status == 200 and body.get("token"):
            tokens[name] = body["token"]
    for service in SERVICE_TICKETS:
        status, body = gw.json("POST", f"/api/v1/rooms/{room_id}/tickets/{service}", host)
        issued[f"ticket:{service}"] = status
        if status == 200 and body.get("token"):
            tokens[f"ticket:{service}"] = body["token"]
    tokens["sesion-invitado"] = guest
    for service in SERVICE_TICKETS:  # tickets del invitado (E4); analytics debe dar 403 (AC3)
        status, body = gw.json("POST", f"/api/v1/rooms/{room_id}/tickets/{service}", guest)
        issued[f"invitado-ticket:{service}"] = status
        if status == 200 and body.get("token"):
            tokens[f"invitado-ticket:{service}"] = body["token"]
    now = int(time.time())
    tokens["legado-tint:room"] = sign_hs256({
        "sub": f"legacy-{uuid.uuid4().hex[:8]}", "room_id": room_id, "role": "host", "nick": "Legado",
        "jti": str(uuid.uuid4()), "iss": issuer, "aud": "tint:room", "iat": now, "exp": now + 600}, secret)
    return tokens, issued


def issue_latency(gw: Gateway, hosts: list[tuple[str, str]], path_for: str, n: int) -> dict:
    """p50/p95/p99 de emitir un ticket, en ronda entre hosts (no satura el límite por usuario)."""
    samples, statuses = [], {}
    for i in range(n):
        token, room_id = hosts[i % len(hosts)]
        path = f"/api/v1/rooms/{room_id}/{path_for}"
        start = time.perf_counter()
        status, _ = gw.request("POST", path, token)
        elapsed = (time.perf_counter() - start) * 1000
        statuses[str(status)] = statuses.get(str(status), 0) + 1
        if status == 200:
            samples.append(elapsed)
    out = {"ruta": f"POST /api/v1/rooms/:id/{path_for}", "n": n, "estados": statuses}
    if samples:
        out.update({"p50_ms": round(statistics.median(samples), 2), "p95_ms": round(percentile(samples, 95), 2),
                    "p99_ms": round(percentile(samples, 99), 2), "max_ms": round(max(samples), 2)})
    return out


def read_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ip", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--env-file", required=True, help=".env del stack (JWT_SECRET y JWT_ISSUER para el token legado)")
    parser.add_argument("--issue-n", type=int, default=200, help="peticiones para el p95 de emisión")
    parser.add_argument("--issue-hosts", type=int, default=10)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    env = read_env(Path(args.env_file))
    gw = Gateway(args.ip, args.port)
    host, room_id, guest = new_host(gw, "matriz")
    tokens, issued = obtain_tickets(gw, host, guest, room_id, env["JWT_SECRET"], env.get("JWT_ISSUER", "tint-identity"))
    matrix = {name: {entry: present(gw, token, entry, room_id) for entry in ENTRIES} for name, token in tokens.items()}
    for name, token in tokens.items():
        matrix[name]["_aud"] = jwt_claims(token).get("aud")

    print("ticket".ljust(28) + "aud".ljust(16) + "".join(e.ljust(12) for e in ENTRIES))
    for name, row in matrix.items():
        cells = "".join(f"{row[e]['resultado'][:3]}:{row[e]['http']}{'/' + str(row[e].get('cierre')) if row[e].get('cierre') else ''}".ljust(12)
                        for e in ENTRIES)
        print(name.ljust(28) + str(row["_aud"]).ljust(16) + cells)

    rows = {k: {e: v for e, v in row.items() if e != "_aud"} for k, row in matrix.items()}
    summary = crossed_summary(rows)
    print("resumen:", json.dumps(summary, ensure_ascii=False))

    pool = [(host, room_id)] + [new_host(gw, f"emision-{i}")[:2] for i in range(args.issue_hosts - 1)]
    latency = {}
    if issued.get("ticket:sync") == 200:
        latency["ticket_sync"] = issue_latency(gw, pool, "tickets/sync", args.issue_n)
    if issued.get("sync-ticket") == 200:
        latency["sync_ticket"] = issue_latency(gw, pool, "sync-ticket", args.issue_n)
    print("emisión:", json.dumps(latency, ensure_ascii=False))

    result = {"taken_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "port": args.port,
              "jwt_legacy_audience": env.get("JWT_LEGACY_AUDIENCE"), "emision_endpoints": issued,
              "matriz": matrix, "resumen": summary, "latencia_emision": latency}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
