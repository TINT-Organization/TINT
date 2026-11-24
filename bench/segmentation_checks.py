#!/usr/bin/env python3
"""Comprobaciones del escenario 3 (segmentación de red) desde DENTRO de los contenedores.

Responde dos preguntas con la misma prueba antes y después de segmentar:
  1. ¿Qué almacenes (PostgreSQL, Redis, MongoDB, MinIO) alcanza un contenedor comprometido de la
     capa de presentación (`web`) y cuáles alcanza una API (`identity-api`, que sí debe alcanzarlos)?
  2. ¿Cuánto tarda un PING a Redis desde la API? (equivalente a `redis-cli --latency` desde una API:
     las imágenes de las APIs no traen redis-cli, así que se mide con un socket de Node.)

Lo orquesta `bench/run.py segmentation --label <etiqueta>`. Aquí solo hay lógica pura y los scripts
de Node; la forma de ejecutarlos en el servidor la inyecta quien llama (`runner`).
"""

from __future__ import annotations

import base64
import json
import math

CLIENTS = ("web", "identity-api")
STORES = {"postgres": 5432, "redis": 6379, "mongodb": 27017, "minio": 9000}
PING_SAMPLES = 1000

_DNS = {"ENOTFOUND", "EAI_AGAIN"}
_TIMEOUT = {"ETIMEDOUT", "timeout"}


def classify(code: str) -> str:
    """Código de error de Node → resultado legible."""
    if code in _DNS:
        return "dns"
    if code == "ECONNREFUSED":
        return "refused"
    if code in _TIMEOUT:
        return "timeout"
    return "error"


def parse_json_line(output: str) -> dict:
    """Última línea JSON de la salida; si no hay ninguna, un error con el texto recibido."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                break
    return {"result": "error", "detail": output.strip()[:200]}


def summarize_samples(samples: list[float]) -> dict:
    """n, media, p50/p95/p99 (interpolación lineal) y máximo, en las unidades de las muestras."""
    if not samples:
        return {"n": 0}
    ordered = sorted(samples)

    def pct(q: float) -> float:
        k = (len(ordered) - 1) * q
        low, high = math.floor(k), math.ceil(k)
        return ordered[low] + (ordered[high] - ordered[low]) * (k - low)

    return {
        "n": len(ordered),
        "mean": round(sum(ordered) / len(ordered), 4),
        "p50": round(pct(0.50), 4),
        "p95": round(pct(0.95), 4),
        "p99": round(pct(0.99), 4),
        "max": ordered[-1],
    }


def probe_js(host: str, port: int) -> str:
    """Script de Node: intenta una conexión TCP y dice si conectó, no resolvió el nombre, etc."""
    return (
        f"const HOST='{host}';const PORT={port};"
        "const net=require('net');const t0=process.hrtime.bigint();"
        "const s=net.connect({host:HOST,port:PORT});s.setTimeout(3000);"
        "const done=(r,c)=>{console.log(JSON.stringify({host:HOST,port:PORT,result:r,code:c,"
        "ms:Number(process.hrtime.bigint()-t0)/1e6}));s.destroy();process.exit(0)};"
        "s.on('connect',()=>done('connected'));"
        "s.on('timeout',()=>done('error','timeout'));"
        "s.on('error',e=>done('error',e.code));"
    )


def ping_js(host: str = "redis", port: int = 6379, samples: int = PING_SAMPLES) -> str:
    """Script de Node: `samples` PING secuenciales a Redis; imprime los RTT en milisegundos."""
    return (
        f"const N={samples};const net=require('net');const out=[];"
        f"const s=net.connect({{host:'{host}',port:{port}}});s.setTimeout(5000);"
        "let t0;const send=()=>{t0=process.hrtime.bigint();s.write('*1\\r\\n$4\\r\\nPING\\r\\n')};"
        "s.on('connect',send);"
        "s.on('data',()=>{out.push(Number(process.hrtime.bigint()-t0)/1e6);"
        "if(out.length<N){send()}else{console.log(JSON.stringify({result:'ok',samples:out}));s.destroy();process.exit(0)}});"
        "s.on('timeout',()=>{console.log(JSON.stringify({result:'error',code:'timeout'}));process.exit(0)});"
        "s.on('error',e=>{console.log(JSON.stringify({result:'error',code:e.code}));process.exit(0)});"
    )


def exec_command(service: str, js: str) -> str:
    """Argumentos de `docker compose` para correr `js` en `service`.

    El script viaja en base64: pasa por ssh y por cmd.exe de Windows, que rompen las comillas y los
    símbolos `& | < > ^ %` de cualquier JavaScript.
    """
    payload = base64.b64encode(js.encode()).decode()
    return f"exec -T {service} node -e \"eval(Buffer.from('{payload}','base64').toString())\""


def _outcome(parsed: dict) -> str:
    if parsed.get("result") == "error" and parsed.get("code"):
        return classify(parsed["code"])
    return parsed.get("result", "error")


def run_checks(runner) -> dict:
    """Matriz cliente → almacén y latencia de PING. `runner(servicio, js) -> stdout`."""
    reachability: dict[str, dict] = {}
    for client in CLIENTS:
        reachability[client] = {}
        for store, port in STORES.items():
            parsed = parse_json_line(runner(client, probe_js(store, port)))
            reachability[client][store] = {"port": port, "result": _outcome(parsed), "ms": parsed.get("ms")}

    ping = parse_json_line(runner("identity-api", ping_js()))
    samples = ping.get("samples", []) if ping.get("result") == "ok" else []
    web = reachability["web"]
    api = reachability["identity-api"]
    return {
        "reachability": reachability,
        "redis_ping_ms_from_identity_api": summarize_samples(samples),
        "summary": {
            "web_reaches_a_store": any(v["result"] == "connected" for v in web.values()),
            "identity_api_reaches_all_stores": all(v["result"] == "connected" for v in api.values()),
        },
    }
