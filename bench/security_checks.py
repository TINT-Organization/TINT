#!/usr/bin/env python3
"""Comprobaciones de seguridad del banco (escenarios 1 a 3 del Prototipo 3), sin carga.

Uso:  python3 bench/security_checks.py --ip 192.168.1.7 --https-port 8443 --out results/antes-develop/security.json

Corre desde el portátil, es decir, desde fuera del servidor: mide lo que ve un atacante en la red.
Requiere nmap y curl; testssl.sh es opcional (si falta, se omite esa parte y queda dicho).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DB_PORTS = {5432: "postgres", 6379: "redis", 27017: "mongodb", 9000: "minio-api", 9001: "minio-consola"}
TLS_ORDER = ["SSLv2", "SSLv3", "TLS1", "TLS1_1", "TLS1_2", "TLS1_3"]
LEGACY = {"SSLv2", "SSLv3", "TLS1", "TLS1_1"}


def parse_nmap_open_ports(grepable: str) -> list[int]:
    """Puertos TCP abiertos en la salida `nmap -oG -`."""
    ports: list[int] = []
    for line in grepable.splitlines():
        if line.startswith("Host:") and "Ports:" in line:
            for item in line.split("Ports:", 1)[1].split(","):
                fields = item.strip().split("/")
                if len(fields) > 2 and fields[1] == "open" and fields[2] == "tcp":
                    ports.append(int(fields[0]))
    return sorted(ports)


def testssl_protocols(findings: list[dict]) -> dict:
    """Protocolos ofrecidos según el JSON de testssl.sh y si hay alguno anterior a TLS 1.2."""
    offered = [
        f["id"]
        for f in findings
        if f.get("id") in TLS_ORDER and str(f.get("finding", "")).lower().startswith("offered")
    ]
    offered.sort(key=TLS_ORDER.index)
    return {"offered": offered, "legacy_offered": sorted(set(offered) & LEGACY)}


def host_token(project: str, sub: str = "bench-security") -> str:
    """ID token del Firebase Auth Emulator (alg none); solo vale contra el stack de pruebas."""

    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    now = int(time.time())
    claims = {"iss": f"https://securetoken.google.com/{project}", "aud": project, "sub": sub, "iat": now, "exp": now + 600}
    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}."


def curl(args: list[str], resolve: str = "") -> subprocess.CompletedProcess:
    extra = ["--resolve", resolve] if resolve else []
    return subprocess.run(["curl", "-sk", "--max-time", "8", *extra, *args], capture_output=True, text=True)


def http_status(url: str, headers: list[str] | None = None, method: str = "GET", resolve: str = "") -> int:
    cmd = ["-o", "/dev/null", "-w", "%{http_code}", "-X", method]
    for header in headers or []:
        cmd += ["-H", header]
    out = curl([*cmd, url], resolve).stdout.strip()
    return int(out) if out.isdigit() else 0


def make_host_shim(directory: Path, ip: str) -> Path:
    """`host` mínimo: testssl.sh exige una herramienta DNS y aquí el destino ya se conoce por IP."""
    shim = directory / "host"
    shim.write_text(f'#!/bin/sh\necho "localhost has address {ip}"\n')
    shim.chmod(0o755)
    return shim


def tcp_open(ip: str, port: int) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=3):
            return True
    except OSError:
        return False


def run_checks(ip: str, https_port: int, project: str) -> dict:
    base = f"https://localhost:{https_port}"  # el sitio de Caddy es https://localhost
    resolve = f"localhost:{https_port}:{ip}"
    result: dict = {"target": ip, "taken_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    scan = subprocess.run(["nmap", "-Pn", "-p-", "--min-rate", "2000", "-oG", "-", ip], capture_output=True, text=True)
    open_ports = parse_nmap_open_ports(scan.stdout)
    result["open_ports"] = open_ports
    result["db_ports_open"] = {name: port in open_ports for port, name in DB_PORTS.items()}

    token = host_token(project)
    internal = f"{base}/api/v1/internal/rooms/x/disable"
    result["internal_route"] = {
        "sin_token": http_status(internal, method="POST", resolve=resolve),
        "con_token_de_host": http_status(internal, [f"Authorization: Bearer {token}"], method="POST", resolve=resolve),
    }

    headers = curl(["-I", f"{base}/healthz"], resolve).stdout.lower()
    result["hsts_present"] = "strict-transport-security" in headers
    result["server_header_exposed"] = "server:" in headers
    result["plain_http_media_9000"] = http_status(f"http://{ip}:9000/minio/health/live") != 0
    result["direct_db_tcp"] = {name: tcp_open(ip, port) for port, name in DB_PORTS.items() if port != 9000}

    testssl = shutil.which("testssl.sh")
    if testssl:
        workdir = Path(tempfile.mkdtemp(prefix="bench-testssl-"))
        out = workdir / "testssl.json"
        env = dict(os.environ)
        if not any(shutil.which(tool) for tool in ("dig", "host", "drill", "nslookup")):
            make_host_shim(workdir, ip)
            env["PATH"] = f"{workdir}{os.pathsep}{env['PATH']}"
        subprocess.run([testssl, "--quiet", "--protocols", "--ip", ip, "--jsonfile", str(out), f"localhost:{https_port}"],
                       capture_output=True, text=True, timeout=300, env=env)
        result["tls"] = testssl_protocols(json.loads(out.read_text())) if out.exists() else {"error": "sin salida"}
    else:
        result["tls"] = {"skipped": "testssl.sh no está instalado"}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", required=True)
    parser.add_argument("--https-port", type=int, default=8443)
    parser.add_argument("--project", default="demo-tint", help="FIREBASE_PROJECT_ID del stack de pruebas")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    result = run_checks(args.ip, args.https_port, args.project)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
