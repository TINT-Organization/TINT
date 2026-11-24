#!/usr/bin/env python3
"""Tablas de la evolución por etapas (E0…E4) a partir de las corridas de `run.py measure`.

  python3 bench/evolution.py bench/results/evo e0 e1 e2 e3 e4 [--json out.json]

Cada etapa es un directorio `<base>/<etapa>/run-N/` (api.json, burst.json, sync.json, stats-*.csv).
Imprime, por métrica, la mediana y el rango mín–máx de las corridas en una columna por etapa. La CPU
de identity-api suma sus réplicas (desde E3 hay dos).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import summarize  # noqa: E402

# (clave, etiqueta, factor) en el orden en que salen en la tabla.
METRICS = [
    ("api.http_req_duration.p50", "API p50 (ms)", 1),
    ("api.http_req_duration.p95", "API p95 (ms)", 1),
    ("api.http_req_duration.p99", "API p99 (ms)", 1),
    ("api.http_reqs.rate", "API req/s servidas", 1),
    ("api.http_req_failed.rate", "API fallos (%)", 100),
    ("api.status_429.count", "API 429 (cuenta)", 1),
    ("api.status_5xx.count", "API 5xx (cuenta)", 1),
    ("api.dropped_iterations.count", "API iteraciones perdidas", 1),
    ("burst.status_429.count", "Ráfaga 429", 1),
    ("burst.status_2xx.count", "Ráfaga 2xx", 1),
    ("burst.status_5xx.count", "Ráfaga 5xx", 1),
    ("burst.http_req_duration.p95", "Ráfaga p95 (ms)", 1),
    ("sync.sync_broadcast_latency_ms.p99", "Sync difusión p99 (ms)", 1),
    ("sync.sync_drift_ms.p99", "Sync drift p99 (ms)", 1),
    ("sync.ws_connecting.p95", "ws_connecting p95 (ms)", 1),
    ("sync.checks.rate", "Sync checks (%)", 100),
    ("cpu.api.reverse-proxy", "CPU nginx, fase API (% medio)", 1),
    ("cpu.api.gateway", "CPU gateway, fase API (% medio)", 1),
    ("cpu.api.identity-api", "CPU identity-api, fase API (% medio, réplicas sumadas)", 1),
    ("cpu.api.postgres", "CPU postgres, fase API (% medio)", 1),
    ("cpu.sync.sync-core", "CPU sync-core, fase Sync (% medio)", 1),
    ("cpu.sync.gateway", "CPU gateway, fase Sync (% medio)", 1),
    ("mem.api.identity-api", "Memoria identity-api (MiB máx, réplicas sumadas)", 1),
    ("mem.api.gateway", "Memoria gateway (MiB máx)", 1),
]
CONTAINER = re.compile(r"^res\.(?P<phase>[^.]+)\.(?P<project>[a-z0-9]+)-(?P<service>[a-z0-9-]+?)-\d+\.(?P<stat>cpu_mean|mem_max_mib)$")


def derived(run: dict[str, float]) -> dict[str, float]:
    """Suma por servicio (réplicas) la CPU media y la memoria máxima de `docker stats`."""
    out = dict(run)
    for name, value in run.items():
        m = CONTAINER.match(name)
        if not m:
            continue
        prefix = "cpu" if m["stat"] == "cpu_mean" else "mem"
        key = f"{prefix}.{m['phase']}.{m['service']}"
        out[key] = round(out.get(key, 0.0) + value, 2)
    return out


def stage_stats(stage_dir: Path) -> dict[str, dict[str, float]]:
    runs = [derived(summarize.load_run(d)) for d in sorted(stage_dir.glob("run-*")) if d.is_dir()]
    return summarize.aggregate(runs) if runs else {}


def fmt(stat: dict[str, float] | None, factor: float) -> str:
    if not stat:
        return "pendiente"
    med, lo, hi = (stat[k] * factor for k in ("median", "min", "max"))
    digits = 0 if max(abs(med), abs(hi)) >= 100 else 1 if max(abs(med), abs(hi)) >= 10 else 2
    if lo == hi:
        return f"{med:.{digits}f}"
    return f"{med:.{digits}f} ({lo:.{digits}f}–{hi:.{digits}f})"


def table(stats: dict[str, dict], stages: list[str]) -> str:
    rows = ["| Métrica | " + " | ".join(s.upper() for s in stages) + " |", "| :--- |" + " ---: |" * len(stages)]
    for key, label, factor in METRICS:
        rows.append(f"| {label} | " + " | ".join(fmt(stats[s].get(key), factor) for s in stages) + " |")
    runs = [str(stats[s].get("_runs", 0)) for s in stages]
    rows.append("| Corridas | " + " | ".join(runs) + " |")
    return "\n".join(rows) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    out_json = None
    if "--json" in argv:
        i = argv.index("--json")
        out_json = Path(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    base, stages = Path(argv[0]), argv[1:]
    stats = {}
    for stage in stages:
        stats[stage] = stage_stats(base / stage)
        stats[stage]["_runs"] = len([d for d in (base / stage).glob("run-*") if d.is_dir()])
    print(table(stats, stages))
    if out_json:
        out_json.write_text(json.dumps({s: {k: v for k, v in st.items()} for s, st in stats.items()}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
