#!/usr/bin/env python3
"""Conmutación por error de identity-api (ADR 0032) medida por la entrada real.

Modo sondeo (por defecto): un flujo constante de `GET /api/v1/healthz` (p. ej. 400 peticiones, una
cada 25 ms) por el gateway; a los `--kill-at` segundos detiene una réplica de identity-api con
SIGTERM y anota cuántas respuestas fueron 200 y cuántas fallaron, y la ventana de fallos.

Modo carga (`--k6-rate`): la mezcla de la API del banco (bench/k6/api.js) a tasa fija y, a los
`--kill-at` segundos, la misma detención; informa el % de errores total y por segundo.

  python3 bench/failover.py --project tintevo --port 8443 --out bench/results/failover-e4.json
  python3 bench/failover.py --project tintevo --port 8443 --k6-rate 60 --k6-duration 60s --kill-at 20 \\
      --out bench/results/failover-k6-e4.json

Solo biblioteca estándar (k6 corre en su imagen de Docker si no está instalado).
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import math
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
K6_IMAGE = "grafana/k6:2.4.0"


def summarize_probes(samples: list[tuple[float, int]], kill_at: float) -> dict:
    """samples: (segundos desde el inicio, estado HTTP; 0 = sin respuesta). Ventana de fallos tras la detención."""
    ok = sum(1 for _, status in samples if status == 200)
    failed = [(t, status) for t, status in samples if status != 200]
    by_status: dict[str, int] = {}
    for _, status in samples:
        by_status[str(status)] = by_status.get(str(status), 0) + 1
    out = {"total": len(samples), "ok": ok, "fallidas": len(failed), "estados": by_status,
           "pct_ok": round(100.0 * ok / len(samples), 2) if samples else None}
    after = [t for t, _ in failed if t >= kill_at]
    if after:
        out["primer_fallo_s"] = round(min(after) - kill_at, 3)
        out["ultimo_fallo_s"] = round(max(after) - kill_at, 3)
    # Recuperación: primera respuesta 200 tras el último fallo.
    if failed:
        last = max(t for t, _ in failed)
        recovered = [t for t, status in samples if status == 200 and t > last]
        out["recupera_s"] = round(min(recovered) - kill_at, 3) if recovered else None
    return out


def failures_per_second(rows: list[tuple[float, bool]], start: float) -> list[dict]:
    """rows: (epoch, falló) de cada petición de k6 → [{segundo, total, fallidas}]."""
    buckets: dict[int, list[int]] = {}
    for epoch, failed in rows:
        second = math.floor(epoch - start)
        bucket = buckets.setdefault(second, [0, 0])
        bucket[0] += 1
        bucket[1] += int(failed)
    return [{"s": s, "total": t, "fallidas": f} for s, (t, f) in sorted(buckets.items())]


# --- E/S -----------------------------------------------------------------------------------------


def replicas(project: str) -> list[str]:
    out = subprocess.run(["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
                          "--filter", "label=com.docker.compose.service=identity-api", "--format", "{{.Names}}"],
                         capture_output=True, text=True, check=True).stdout.split()
    return sorted(out)


def stop_replica(name: str) -> dict:
    started = time.time()
    subprocess.run(["docker", "kill", "--signal", "SIGTERM", name], capture_output=True, text=True, check=True)
    return {"contenedor": name, "senal": "SIGTERM", "epoch": round(started, 3)}


def probe(ip: str, port: int, path: str, timeout: float) -> int:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(ip, port, context=ctx, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Host": f"localhost:{port}"})
        resp = conn.getresponse()
        resp.read()
        return resp.status
    except (OSError, http.client.HTTPException):
        return 0
    finally:
        conn.close()


def run_probes(args: argparse.Namespace) -> dict:
    before = replicas(args.project)
    if not before:
        raise SystemExit(f"no hay réplicas de identity-api en el proyecto {args.project}")
    samples: list[tuple[float, int]] = []
    lock = threading.Lock()
    start = time.time()
    victim: dict = {}

    def one(i: int) -> None:
        delay = start + i * args.every_ms / 1000.0 - time.time()
        if delay > 0:
            time.sleep(delay)
        sent = time.time() - start
        status = probe(args.ip, args.port, args.path, args.timeout)
        with lock:
            samples.append((sent, status))

    def killer() -> None:
        time.sleep(args.kill_at)
        victim.update(stop_replica(before[0]))

    threading.Thread(target=killer, daemon=True).start()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(one, range(args.requests)))
    samples.sort()
    time.sleep(1)
    return {"modo": "sondeo", "ruta": args.path, "peticiones": args.requests, "cada_ms": args.every_ms,
            "detencion_a_s": args.kill_at, "replicas_antes": before, "replicas_despues": replicas(args.project),
            "detenida": victim, "resultado": summarize_probes(samples, args.kill_at)}


def run_k6(args: argparse.Namespace) -> dict:
    before = replicas(args.project)
    out_dir = Path(args.out).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(args.out).stem
    summary, rows_csv = out_dir / f"{stem}.k6.json", out_dir / f"{stem}.k6.csv"
    env = {"BASE_URL": f"https://localhost:{args.port}", "SERVER_IP": args.ip, "RATE": str(args.k6_rate),
           "DURATION": args.k6_duration, "HOSTS": str(args.hosts)}
    run = ["run", "--insecure-skip-tls-verify", "--summary-trend-stats=avg,min,med,p(90),p(95),p(99),max"]
    if shutil.which("k6"):
        cmd = ["k6", *run, "--summary-export", str(summary), "--out", f"csv={rows_csv}"]
        script = str(ROOT / "bench/k6/api.js")
    else:
        cmd = ["docker", "run", "--rm", "--network", "host", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{ROOT}:/work",
               "-w", "/work", K6_IMAGE, *run, "--summary-export", f"/work/{summary.relative_to(ROOT)}",
               "--out", f"csv=/work/{rows_csv.relative_to(ROOT)}"]
        script = "bench/k6/api.js"
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    proc = subprocess.Popen([*cmd, script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT)
    # El setup de k6 siembra hosts y salas antes de la carga: se espera su primera línea de progreso.
    started = None
    log_lines = []
    assert proc.stdout is not None
    victim: dict = {}
    for line in proc.stdout:
        log_lines.append(line)
        if started is None and "running (" in line:
            started = time.time()
            threading.Timer(args.kill_at, lambda: victim.update(stop_replica(before[0]))).start()
    proc.wait()
    (out_dir / f"{stem}.k6.log").write_text("".join(log_lines))
    rows: list[tuple[float, bool]] = []
    with rows_csv.open() as fh:
        for row in csv.DictReader(fh):
            if row["metric_name"] == "http_req_failed" and row.get("scenario") == "api":
                rows.append((float(row["timestamp"]), row["metric_value"] == "1"))
    values = json.loads(summary.read_text())["metrics"]
    failed = values.get("http_req_failed", {})
    kill_epoch = victim.get("epoch", 0)
    timeline = failures_per_second(rows, kill_epoch) if kill_epoch else []
    rows_csv.unlink(missing_ok=True)  # pesado; queda el resumen por segundo
    return {"modo": "carga", "tasa": args.k6_rate, "duracion": args.k6_duration, "detencion_a_s": args.kill_at,
            "replicas_antes": before, "replicas_despues": replicas(args.project), "detenida": victim,
            "k6_exit": proc.returncode, "peticiones": len(rows),
            "fallidas": sum(1 for _, f in rows if f),
            "pct_fallidas": round(100.0 * sum(1 for _, f in rows if f) / len(rows), 3) if rows else None,
            "http_req_failed": failed, "status_5xx": values.get("status_5xx", {}), "status_429": values.get("status_429", {}),
            "por_segundo_desde_la_detencion": timeline}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ip", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--project", required=True, help="proyecto de Compose del banco")
    parser.add_argument("--path", default="/api/v1/healthz")
    parser.add_argument("--requests", type=int, default=400)
    parser.add_argument("--every-ms", type=float, default=25.0)
    parser.add_argument("--kill-at", type=float, default=3.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--k6-rate", type=int, default=0, help="si > 0, modo carga con bench/k6/api.js a esta tasa")
    parser.add_argument("--k6-duration", default="60s")
    parser.add_argument("--hosts", type=int, default=40)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    result = run_k6(args) if args.k6_rate else run_probes(args)
    result["taken_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    brief = {k: v for k, v in result.items() if k != "por_segundo_desde_la_detencion"}
    print(json.dumps(brief, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
