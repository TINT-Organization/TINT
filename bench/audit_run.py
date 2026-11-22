#!/usr/bin/env python3
"""Escenario 5 (bitácora de auditoría): acciones sensibles hechas vs registradas, y su latencia.

Corre contra el stack local (`docker compose up -d`), no contra el servidor del equipo. Solo usa la
biblioteca estándar.

  python3 bench/audit_run.py run --label despues            # una corrida: k6 + conteo en audit_entries
  python3 bench/audit_run.py summary antes despues          # mediana de las corridas de cada etiqueta

`run` lanza bench/k6/audit.js (con `k6` si está instalado; si no, con la imagen de Docker), espera a
que audit-log termine de guardar y cuenta en PostgreSQL las filas de los actores de esa corrida.
El 401 sin token (`access.denied.401`) no trae actor: se cuenta por hora, entre el inicio y el fin
de la corrida. Resultados en bench/results/audit/<etiqueta>/run-N.json y k6-run-N.json (no se versionan).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import summarize  # noqa: E402

BENCH = Path(__file__).parent
ROOT = BENCH.parent
RESULTS = BENCH / "results" / "audit"
K6_IMAGE = "grafana/k6:0.54.0"
ACTIONS = [
    "room.create",
    "room.update",
    "invitation.create",
    "ban.create",
    "ban.delete",
    "guest.revoke",
    "invitation.revoke",
    "access.denied",
    "access.denied.401",
    "room.delete",
]
LABEL = re.compile(r"^[a-z0-9-]{1,20}$")


def metric(action: str) -> str:
    return action.replace(".", "_")


def psql(sql: str) -> list[list[str]]:
    """Consulta en el contenedor de Postgres; `[]` si la tabla aún no existe (antes de la bitácora)."""
    cmd = ["docker", "compose", "exec", "-T", "postgres", "sh", "-c",
           'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F "|" -v ON_ERROR_STOP=1 -c "$1"', "sh", sql]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if result.returncode != 0:
        if "does not exist" in result.stderr:
            return []
        raise RuntimeError(f"psql falló: {result.stderr.strip()[:300]}")
    return [line.split("|") for line in result.stdout.splitlines() if line]


def recorded(run_id: str, start: str, end: str) -> dict[str, int]:
    counts = {action: 0 for action in ACTIONS}
    by_actor = psql(
        "SELECT action, count(*) FROM audit_entries "
        f"WHERE actor_id LIKE 'bench-audit-{run_id}-%' GROUP BY action"
    )
    for action, n in by_actor:
        counts[action] = counts.get(action, 0) + int(n)
    anonymous = psql(
        "SELECT count(*) FROM audit_entries WHERE action = 'access.denied' AND actor_id IS NULL "
        f"AND occurred_at BETWEEN '{start}' AND '{end}'"
    )
    counts["access.denied.401"] = int(anonymous[0][0]) if anonymous else 0
    return counts


def wait_until_stable(run_id: str, start: str, timeout: float = 60.0) -> None:
    """audit-log guarda de forma asíncrona: espera a que el conteo deje de crecer 3 s seguidos."""
    last, stable_since, deadline = -1, time.monotonic(), time.monotonic() + timeout
    while time.monotonic() < deadline:
        total = sum(recorded(run_id, start, now_iso()).values())
        if total != last:
            last, stable_since = total, time.monotonic()
        elif time.monotonic() - stable_since >= 3:
            return
        time.sleep(1)
    print(f"   aviso: el conteo siguió creciendo después de {timeout:.0f} s")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def k6_command(env: dict[str, str], export: Path) -> list[str]:
    args = ["run", "--quiet", "--summary-export", str(export)]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    if shutil.which("k6"):
        return ["k6", *args, str(BENCH / "k6" / "audit.js")]
    # Sin k6 instalado: la imagen oficial, en la red del host para llegar a https://localhost.
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    gid = subprocess.run(["id", "-g"], capture_output=True, text=True).stdout.strip()
    inner = [a.replace(str(BENCH), "/bench") for a in args]
    return ["docker", "run", "--rm", "--network", "host", "--user", f"{uid}:{gid}",
            "-v", f"{BENCH}:/bench", K6_IMAGE, *inner, "/bench/k6/audit.js"]


def run(args: argparse.Namespace) -> int:
    out_dir = RESULTS / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    n = len(list(out_dir.glob("run-*.json"))) + 1
    run_id = f"{args.label}-{int(time.time())}"
    export = out_dir / f"k6-run-{n}.json"
    env = {"BASE_URL": args.base_url, "RUN_ID": run_id, "RATE": str(args.rate), "DURATION": args.duration}

    print(f"== corrida {n} de '{args.label}' (RUN_ID={run_id}, {args.rate} recorridos/s, {args.duration})")
    start = now_iso()
    k6 = subprocess.run(k6_command(env, export), cwd=ROOT)
    print("   esperando a que audit-log termine de guardar…")
    wait_until_stable(run_id, start)
    end = now_iso()

    values = summarize.k6_values(json.loads(export.read_text()))
    done = {a: int(values.get(f"n_{metric(a)}.count", 0)) for a in ACTIONS}
    logged = recorded(run_id, start, end)
    result = {
        "label": args.label,
        "run_id": run_id,
        "start": start,
        "end": end,
        "k6_exit": k6.returncode,
        "pasos_fallidos": int(values.get("pasos_fallidos.count", 0)),
        "hechas": done,
        "registradas": logged,
        "latencia_ms": {k: v for k, v in values.items() if k.startswith("t_") and k.split(".")[-1] in ("p50", "p95", "p99")},
    }
    (out_dir / f"run-{n}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print_run(result)
    return 0


def print_run(result: dict) -> None:
    print(f"\n{'acción':<20}{'hechas':>8}{'registradas':>13}")
    for action in ACTIONS:
        print(f"{action:<20}{result['hechas'][action]:>8}{result['registradas'][action]:>13}")
    covered = [a for a in ACTIONS if a != "access.denied.401"]
    total_done = sum(result["hechas"][a] for a in covered)
    total_logged = sum(min(result["registradas"][a], result["hechas"][a]) for a in covered)
    print(f"{'total (sin el 401)':<20}{total_done:>8}{total_logged:>13}")
    lat = result["latencia_ms"]
    print(f"\nt_sensible: p50 {lat.get('t_sensible.p50', 0):.1f} ms · p95 {lat.get('t_sensible.p95', 0):.1f} ms · "
          f"p99 {lat.get('t_sensible.p99', 0):.1f} ms · pasos fallidos {result['pasos_fallidos']}")


def summary(args: argparse.Namespace) -> int:
    for label in args.labels:
        runs = [json.loads(p.read_text()) for p in sorted((RESULTS / label).glob("run-*.json"))]
        if not runs:
            print(f"sin corridas en {RESULTS / label}")
            continue
        flat = [{**r["latencia_ms"], **{f"hechas.{a}": v for a, v in r["hechas"].items()},
                 **{f"registradas.{a}": v for a, v in r["registradas"].items()}} for r in runs]
        print(f"\n== {label} ({len(runs)} corridas; mediana [mín–máx])")
        for name, agg in summarize.aggregate(flat).items():
            if name.startswith("t_") and not name.endswith(".p95"):
                continue
            print(f"   {name:<36}{agg['median']:>10.1f}  [{agg['min']:.1f}–{agg['max']:.1f}]")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="una corrida de k6 y el conteo en la bitácora")
    p_run.add_argument("--label", required=True, help="etiqueta: antes, despues… ([a-z0-9-])")
    p_run.add_argument("--rate", type=int, default=2, help="recorridos por segundo (cada uno 12 peticiones)")
    p_run.add_argument("--duration", default="60s")
    p_run.add_argument("--base-url", default="https://localhost")
    p_sum = sub.add_parser("summary", help="mediana de las corridas de cada etiqueta")
    p_sum.add_argument("labels", nargs="+")
    args = parser.parse_args()
    if args.cmd == "run" and not LABEL.match(args.label):
        parser.error("--label solo admite [a-z0-9-] (máx. 20)")
    return run(args) if args.cmd == "run" else summary(args)


if __name__ == "__main__":
    sys.exit(main())
